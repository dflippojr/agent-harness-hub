// HUB-07 pairing requests, HUB-08 marketplace and HUB-09 accounts and maintenance. Each test makes its own requests
// and members with unique names, so the seeded rows stay as they are for the other projects.
import { appAsksToPair, expect, openSection, test, unique } from "./fixtures.mjs";

// A request's card (Needs you, Waiting for the app) is named by its heading; a History row carries its name in a cell.
const card = (page, name) => page.locator("[data-request-id]").filter({ has: page.getByRole("heading", { name, exact: true }) });
const historyRow = (page, name) => page.locator('[data-group="history"] tr[data-request-id]').filter({ hasText: name });
const notice = (page) => page.locator("main p.notice[aria-live]").first();
// The daemon takes at most 10 pairing requests per source in 10 minutes and 3 pending ones, and the instance seeds
// two. Two requests per project in three projects stay inside that.
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

  await request_.getByLabel("Match code the app shows").fill(asked.match_code === "000000" ? "111111" : "000000");
  await request_.getByRole("button", { name: `Approve ${name}` }).click();
  await expect(notice(page)).toHaveText("the match code does not match the one the App shows");

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
  await expect(request_).toContainText("elevated");
  await request_.getByLabel("Match code the app shows").fill("123456");
  await expect(request_.getByRole("button", { name: `Approve ${name}` })).toBeDisabled();
  await request_.getByRole("checkbox").check();
  await expect(request_.getByRole("button", { name: `Approve ${name}` })).toBeEnabled();

  await request_.getByRole("button", { name: `Deny ${name}` }).click();
  // The question renders inside the card that was clicked (decision 5 of #65).
  const confirm = request_.getByRole("alert").filter({ has: page.getByRole("button", { name: "Confirm deny" }) });
  await expect(confirm.getByRole("heading", { name: `Deny ${name}?` })).toBeVisible();
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(group(page, "Needs you").filter({ has: card(page, name) })).toHaveCount(1);

  await card(page, name).getByRole("button", { name: `Deny ${name}` }).click();
  await card(page, name).getByRole("button", { name: "Confirm deny" }).click();
  await page.reload();
  await expect(historyRow(page, name)).toHaveCount(1);
  await expect(historyRow(page, name).getByText("Denied", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("marketplace: consent can be cancelled; arming lists the slot until it is withdrawn", async ({ signedIn: page, errors }) => {
  await openSection(page, "marketplace");
  await expect(page.getByText("Being listed here grants nothing.")).toBeVisible();
  // Consent, hand-off and the withdraw question all render inside the entry's own card (Hub design 10).
  const entry = page.getByRole("region", { name: "financial-planner", exact: true });
  await entry.getByRole("button", { name: "Set up financial-planner" }).click();
  const consent = entry.getByRole("region", { name: /^Set up financial-planner/ });
  await expect(consent).toContainText("Nothing is armed until you approve.");
  await consent.getByRole("button", { name: "Cancel" }).click();
  await expect(consent).toHaveCount(0);
  await expect(entry.getByRole("button", { name: "Set up financial-planner" })).toBeFocused();

  await entry.getByRole("button", { name: "Set up financial-planner" }).click();
  for (const box of await consent.getByRole("checkbox").all()) await box.check();
  const origins = consent.getByRole("radio");
  if (await origins.count()) await origins.first().check();
  await consent.getByRole("button", { name: "Approve" }).click();
  const handoff = entry.getByRole("region", { name: "Slot armed for financial-planner" });
  await expect(handoff).toBeVisible();
  await handoff.getByRole("button", { name: "Done" }).click();
  await page.reload();
  await entry.getByRole("button", { name: "Withdraw slot for financial-planner" }).click();
  await entry.getByRole("alert").getByRole("button", { name: "Confirm withdraw" }).click();
  await page.reload();
  await expect(entry.getByRole("button", { name: "Set up financial-planner" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Withdraw slot for financial-planner" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("accounts: add a member, then disable it after confirmation", async ({ signedIn: page, errors }, testInfo) => {
  const display = unique(testInfo, "Member");
  const login = `${display.replaceAll(" ", "-").toLowerCase()}@example.test`;
  await openSection(page, "admin");
  // The page's own notice, not the rail's daemon chip or the banner slot.
  const notice = page.locator("main p.notice[role=status]");
  // The members table and the open member's detail pane load on their own; Add member is the page's primary button.
  const pane = page.locator(".admin-member");
  const row = page.getByRole("table", { name: "Members" }).locator("tbody tr").filter({ hasText: display });
  await expect(page.getByRole("table", { name: "Members" })).toContainText("GiB");
  await page.locator("header").getByRole("button", { name: "Add member" }).click();
  await pane.getByLabel(/Exact Tailscale login/).fill(login);
  await pane.getByLabel(/Display name/).fill(display);
  await pane.getByRole("button", { name: "Add member" }).click();
  await expect(notice).toContainText("Daemon accepted add member.");
  await expect(page.getByRole("heading", { name: display, level: 2 })).toBeVisible();
  await page.reload();
  await expect(row).toContainText("Enabled");
  await row.getByRole("button", { name: `Open ${display}` }).click();
  await expect(page.getByRole("heading", { name: display, level: 2 })).toBeVisible();
  const userId = await pane.getAttribute("data-member-id");
  expect(userId).toMatch(/^u-[\w-]+$/);

  await pane.getByRole("button", { name: "Disable", exact: true }).click();
  const confirm = page.getByRole("button", { name: "Confirm update member", exact: true });
  await expect(pane.getByRole("alert", { name: "Confirm action" })).toContainText(userId);
  await confirm.click();
  await expect(notice).toContainText("Daemon accepted update member.");
  await page.reload();
  await expect(row).toContainText("Disabled");
  await row.getByRole("button", { name: `Open ${display}` }).click();
  await expect(pane).toContainText(userId);
  await expect(pane.getByRole("button", { name: "Enable", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
