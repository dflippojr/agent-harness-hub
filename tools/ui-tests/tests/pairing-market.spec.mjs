// HUB-07 pairing requests and HUB-08 marketplace. Each test makes its own requests with unique names, so the seeded rows
// stay as they are for the other projects.
import { appAsksToPair, daemonUrl, expect, functionalProject, openSection, test, unique, writesTo } from "./fixtures.mjs";

// A request's card (Needs you, Waiting for the app) is named by its heading; a History row carries its name in a cell.
const card = (page, name) => page.locator("[data-request-id]").filter({ has: page.getByRole("heading", { name, exact: true }) });
const historyRow = (page, name) => page.locator('[data-group="history"] tr[data-request-id]').filter({ hasText: name });
const notice = (page) => page.locator("main p.notice[aria-live]").first();
// The daemon takes at most 10 pairing requests per source in 10 minutes and 3 pending ones, and the instance seeds
// two. Two requests per project in three projects and the withheld-name one stay inside that; the slot journeys in
// slots.spec.mjs are owner-armed and spend none.
const PAIRING_PROJECTS = ["desktop-light", "desktop-dark", "phone-dark"];
const pairingProject = (testInfo) => test.skip(!PAIRING_PROJECTS.includes(testInfo.project.name),
  "the daemon's per-source pairing request limit");
const group = (page, title) => page.locator("section.pairing-group").filter({ has: page.getByRole("heading", { name: new RegExp(`^${title}`) }) });

test("approve a request with the app's code; a wrong code is refused", async ({ signedIn: page, request, errors }, testInfo) => {
  pairingProject(testInfo);
  const name = unique(testInfo, "Paired notes");
  const asked = await appAsksToPair(request, name);
  // Overview counts the waiting request (other projects' requests may be counted too) and links to the list.
  await openSection(page, "overview");
  const waiting = page.getByRole("status").filter({ has: page.getByRole("heading", { name: /^\d+ pairing requests? needs? you$/ }) });
  await expect(waiting).toContainText(/\d+ to approve/);
  await waiting.getByRole("link", { name: "Review requests" }).click();
  await expect(page).toHaveURL(/#\/pairing$/);
  const request_ = card(page, name);
  await expect(group(page, "Needs you").filter({ has: request_ })).toHaveCount(1);
  await expect(request_.getByText("Needs approval", { exact: true })).toBeVisible();
  await expect(request_).toContainText("This app can start agent tasks on your harness");
  // The daemon gives the code only to the App: the card never carries it before the owner types it.
  expect(await request_.evaluate((el, code) => el.outerHTML.includes(code), asked.match_code)).toBe(false);
  await expect(request_.getByLabel("Match code the app shows")).toHaveValue("");
  await expect(request_).toContainText("If the app shows no code or an error, deny.");
  // What the App said about itself is marked unverified, once per card.
  await expect(request_.locator(".caveat")).toHaveText("Name, origin and catalog id are stated by the app, not verified. " +
    "Only the match code on your app's screen proves this is your app.");
  await expect(request_.locator("dt").filter({ hasText: /^Started from$/ }).locator("+ dd")).toHaveText(/^The app · /);

  await request_.getByLabel("Match code the app shows").fill(asked.match_code === "000000" ? "111111" : "000000");
  await request_.getByRole("button", { name: `Approve ${name}` }).click();
  await expect(notice(page)).toHaveText("The match code does not match the one the App shows.");

  await request_.getByLabel("Match code the app shows").fill(asked.match_code);
  await request_.getByRole("button", { name: `Approve ${name}` }).click();
  await expect(notice(page)).toHaveText(`Approved ${name}. The app now fetches its own token.`);
  await page.reload();
  await expect(group(page, "Waiting for the app").filter({ has: card(page, name) })).toHaveCount(1);
  await expect(card(page, name)).toContainText("Waiting for the app to fetch its own token from the daemon");
  expect(errors.filter((e) => !/status of 400/.test(e))).toEqual([]);
});

test("an elevated request needs the acknowledgement; deny asks first and Cancel keeps it", async ({ signedIn: page, request, errors }, testInfo) => {
  pairingProject(testInfo);
  const name = unique(testInfo, "Elevated notes");
  await appAsksToPair(request, name, ["sessions", "approvals"]);
  await openSection(page, "pairing");
  const request_ = card(page, name);
  const id = await request_.getAttribute("data-request-id");
  await expect(request_.locator(".decision-head .pill")).toHaveText(["Needs approval", "Elevated scope"]);
  await expect(request_.locator('[data-scope="approvals"] .pill')).toHaveText("Elevated");
  // The daemon's own copy, with only its **pairs** in bold.
  await expect(request_.locator('[data-scope="approvals"] .disclosure-text strong')).toHaveText("approve or deny tool calls");
  await request_.getByRole("button", { name: /^Host commands?$/ }).click();
  await expect(request_.getByText(`harness pairing-requests approve ${id} --match <code> --acknowledge-elevated`, { exact: true }))
    .toBeVisible();
  await request_.getByLabel("Match code the app shows").fill("123456");
  const approve = request_.getByRole("button", { name: `Approve ${name}` });
  await expect(approve).toBeDisabled();
  const approvals = writesTo(page, `/api/pairing/${id}/approve`);
  await approve.click({ force: true });
  await request_.getByRole("checkbox").check();
  await expect(approve).toBeEnabled();
  await request_.getByRole("checkbox").uncheck();
  await expect(approve).toBeDisabled();
  expect(approvals).toEqual([]);
  await request_.getByRole("checkbox").check();

  const deny = request_.getByRole("button", { name: `Deny ${name}` });
  await deny.click();
  // The question renders inside the card that was clicked (decision 5 of #65).
  const confirm = request_.getByRole("alert").filter({ has: page.getByRole("button", { name: "Confirm deny" }) });
  await expect(confirm.getByRole("heading", { name: `Deny ${name}?` })).toBeVisible();
  await expect(confirm).toContainText(`Deny pairing request ${id}`);
  await expect(confirm.getByRole("button", { name: "Confirm deny" })).toBeFocused();
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(deny).toBeFocused();
  await expect(request_.getByLabel("Match code the app shows")).toHaveValue("123456");
  await expect(group(page, "Needs you").filter({ has: card(page, name) })).toHaveCount(1);

  await card(page, name).getByRole("button", { name: `Deny ${name}` }).click();
  await card(page, name).getByRole("button", { name: "Confirm deny" }).click();
  await page.reload();
  await expect(historyRow(page, name)).toHaveCount(1);
  await expect(historyRow(page, name).getByText("Denied", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("a token-shaped name is withheld: the card offers no approval, only Deny and the host's list",
  async ({ signedIn: page, request, errors }, testInfo) => {
    // One more request from this source: desktop-light only, to stay inside the daemon's limit.
    test.skip(testInfo.project.name !== "desktop-light", "the daemon's per-source pairing request limit");
    // A display-prefix-shaped word, as an App could send: the Hub withholds the name rather than show it.
    const asked = await appAsksToPair(request, `Notes hp-${Date.now().toString(36)}x`);
    await openSection(page, "pairing");
    const box = page.locator(`[data-request-id="${asked.id}"]`);
    await expect(box.getByRole("heading", { level: 3 })).toHaveText("[withheld: token-shaped]");
    await expect(box.locator('[data-action="approve"], [data-action="confirm"]')).toHaveCount(0);
    await expect(box.locator("input")).toHaveCount(0);
    await expect(box).toContainText("Review this request with harness pairing-requests list on the host.");
    await expect(box.getByText("harness pairing-requests list", { exact: true })).toBeVisible();
    await box.locator('[data-action="deny"]').click();
    await box.getByRole("button", { name: "Confirm deny", exact: true }).click();
    await expect(page.locator(`[data-group="history"] [data-request-id="${asked.id}"][data-state="denied"]`)).toBeVisible();
    expect(errors).toEqual([]);
  });

test("marketplace: consent can be cancelled; arming lists the slot until it is withdrawn", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "marketplace");
  await expect(page.getByText("Being listed here grants nothing.")).toBeVisible();
  // Consent, hand-off and the withdraw question all render inside the entry's own card (Hub design 10).
  const entry = page.getByRole("region", { name: "financial-planner", exact: true });
  const arms = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname === "/api/marketplace/arm") arms.push(r); });
  await entry.getByRole("button", { name: "Set up financial-planner" }).click();
  const consent = entry.getByRole("region", { name: /^Set up financial-planner/ });
  await expect(consent.getByRole("heading", { level: 3 })).toBeFocused();
  await expect(page.locator(".market-consent")).toHaveCount(1);
  await expect(consent).toContainText("Nothing is armed until you approve.");
  await expect(consent).toContainText("Arming grants scope consent.");
  await expect(consent).toContainText("Native app. When it claims the slot, you confirm it here or on Pairing requests");
  await expect(consent.locator('[data-scope="sessions"] .disclosure-text')).toHaveText(/^This app can start agent tasks/);
  await expect(consent).toContainText("Publisher's reason (not verified): ");
  await consent.getByRole("button", { name: /^Host commands?$/ }).click();
  await expect(consent.getByText("harness pairing-requests arm ahpub.dflippojr.financial-planner --scopes sessions",
    { exact: true })).toBeVisible();
  await consent.getByRole("button", { name: "Cancel" }).click();
  await expect(consent).toHaveCount(0);
  await expect(entry.getByRole("button", { name: "Set up financial-planner" })).toBeFocused();
  expect(arms).toEqual([]);

  await entry.getByRole("button", { name: "Set up financial-planner" }).click();
  for (const box of await consent.getByRole("checkbox").all()) await box.check();
  const origins = consent.getByRole("radio");
  if (await origins.count()) await origins.first().check();
  await consent.getByRole("button", { name: "Approve" }).click();
  const handoff = entry.getByRole("region", { name: "Slot armed for financial-planner" });
  await expect(handoff).toBeVisible();
  expect(arms).toHaveLength(1);
  expect(arms[0].headers()["x-hub-request"]).toBe("1");
  expect(arms[0].postDataJSON()).toMatchObject({ catalog_app_id: "ahpub.dflippojr.financial-planner", scopes: ["sessions"] });
  // The hand-off names the slot id and the daemon URL the App needs, each with a copy button.
  const slotId = await entry.locator("[data-slot]").first().getAttribute("data-slot");
  await expect(handoff).toContainText("Give this only to financial-planner; it lets that app claim the slot once.");
  await expect(handoff.getByText(slotId, { exact: true })).toBeVisible();
  await expect(handoff.getByText(daemonUrl(), { exact: true })).toBeVisible();
  await expect(handoff.getByRole("button", { name: `Copy: ${slotId}` })).toBeVisible();
  await expect(entry.locator(`[data-slot="${slotId}"]`)).toContainText("waiting for the app to claim");
  await handoff.getByRole("button", { name: "Done" }).click();
  await page.reload();
  await entry.getByRole("button", { name: "Withdraw slot for financial-planner" }).click();
  await entry.getByRole("alert").getByRole("button", { name: "Confirm withdraw" }).click();
  await page.reload();
  await expect(entry.getByRole("button", { name: "Set up financial-planner" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Withdraw slot for financial-planner" })).toHaveCount(0);
  expect(errors).toEqual([]);
});
