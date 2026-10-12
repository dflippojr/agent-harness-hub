// HUB-07 pairing requests, HUB-08 marketplace and HUB-09 accounts and maintenance. Each test makes its own requests
// and members with unique names, so the seeded rows stay as they are for the other projects.
import { appAsksToPair, expect, openSection, test, unique } from "./fixtures.mjs";

const card = (page, name) => page.locator("section.card").filter({ has: page.getByRole("heading", { name: new RegExp(`^${name} ·`) }) });
const notice = (page) => page.locator("main p.notice[aria-live]").first();
// The daemon takes at most 10 pairing requests per source in 10 minutes and 3 pending ones, and the instance seeds
// two. Two requests per project in three projects stay inside that.
const PAIRING_PROJECTS = ["desktop-light", "desktop-dark", "phone-dark"];
const pairingProject = (testInfo) => test.skip(!PAIRING_PROJECTS.includes(testInfo.project.name),
  "the daemon's per-source pairing request limit");
const group = (page, title) => page.locator("section.pairing-group").filter({ has: page.getByRole("heading", { name: new RegExp(`^${title} \\(`) }) });

test("approve a request with the app's code; a wrong code is refused", async ({ signedIn: page, request, errors }, testInfo) => {
  pairingProject(testInfo);
  const name = unique(testInfo, "Paired notes");
  const asked = await appAsksToPair(request, name);
  await openSection(page, "pairing");
  const request_ = card(page, name);
  await expect(group(page, "Waiting for approval").filter({ has: request_ })).toHaveCount(1);
  await expect(request_).toContainText("This app can start agent tasks on your harness");

  await request_.getByLabel("Match code the app shows").fill(asked.match_code === "000000" ? "111111" : "000000");
  await request_.getByRole("button", { name: `Approve ${name}` }).click();
  await expect(notice(page)).toHaveText("the match code does not match the one the App shows");

  await request_.getByLabel("Match code the app shows").fill(asked.match_code);
  await request_.getByRole("button", { name: `Approve ${name}` }).click();
  await expect(notice(page)).toHaveText(`Approved ${name}. The app now fetches its own token.`);
  await page.reload();
  await expect(group(page, "Approved, waiting for the app").filter({ has: card(page, name) })).toHaveCount(1);
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
  const confirm = page.getByRole("alert").filter({ has: page.getByRole("button", { name: "Confirm deny" }) });
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(group(page, "Waiting for approval").filter({ has: card(page, name) })).toHaveCount(1);

  await card(page, name).getByRole("button", { name: `Deny ${name}` }).click();
  await page.getByRole("button", { name: "Confirm deny" }).click();
  await page.reload();
  await expect(group(page, "Denied or withdrawn").filter({ has: card(page, name) })).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("marketplace: consent can be cancelled; arming lists the slot until it is withdrawn", async ({ signedIn: page, errors }) => {
  await openSection(page, "marketplace");
  await expect(page.getByText("Being listed here grants nothing.")).toBeVisible();
  await page.getByRole("button", { name: "Set up financial-planner" }).click();
  const consent = page.getByRole("region", { name: /^Set up financial-planner/ });
  await expect(consent).toContainText("Nothing is armed until you approve.");
  await consent.getByRole("button", { name: "Cancel" }).click();
  await expect(consent).toHaveCount(0);

  await page.getByRole("button", { name: "Set up financial-planner" }).click();
  for (const box of await consent.getByRole("checkbox").all()) await box.check();
  const origins = consent.getByRole("radio");
  if (await origins.count()) await origins.first().check();
  await consent.getByRole("button", { name: "Approve" }).click();
  const handoff = page.getByRole("region", { name: "Slot armed for financial-planner" });
  await expect(handoff).toBeVisible();
  await handoff.getByRole("button", { name: "Done" }).click();
  await page.reload();
  await page.getByRole("button", { name: "Withdraw slot for financial-planner" }).click();
  await page.getByRole("button", { name: "Confirm withdraw" }).click();
  await page.reload();
  await expect(page.getByRole("button", { name: "Set up financial-planner" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Withdraw slot for financial-planner" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("accounts: add a member, then disable it after confirmation", async ({ signedIn: page, errors }, testInfo) => {
  const display = unique(testInfo, "Member");
  const login = `${display.replaceAll(" ", "-").toLowerCase()}@example.test`;
  await openSection(page, "admin");
  const form = page.locator(".admin-form");
  const choose = page.getByLabel("Choose action", { exact: true });
  await choose.selectOption("account_add");
  await form.getByLabel(/Exact Tailscale login/).fill(login);
  await form.getByLabel(/Display name/).fill(display);
  await form.getByRole("button").click();
  await expect(page.getByRole("status")).toContainText("Daemon accepted add member.");
  await page.reload();
  const member = page.locator(".admin-row").filter({ has: page.getByRole("heading", { name: display }) });
  await expect(member).toBeVisible();
  const userId = /u-[\w-]+/.exec(await member.innerText())?.[0];
  expect(userId).toBeTruthy();

  await choose.selectOption("account_save");
  await form.getByLabel("Member id").fill(userId);
  await form.getByLabel("Member enabled").selectOption("false");
  await form.getByRole("button").click();
  const confirm = page.getByRole("button", { name: "Confirm update member", exact: true });
  await expect(page.getByRole("region", { name: "Confirm action" })).toContainText(userId);
  await confirm.click();
  await expect(page.getByRole("status")).toContainText("Daemon accepted update member.");
  await page.reload();
  await choose.selectOption("account");
  await form.getByLabel("Member id").fill(userId);
  await form.getByRole("button").click();
  await expect(page.locator(".admin-output")).toContainText(/enabled\s*(false|No)/i);
  expect(errors).toEqual([]);
});
