// HUB-09 accounts and maintenance against the real daemon's household store and audit: what each write sends, the
// questions before it, and what the daemon then reports, including its refusals. Members are made per test with
// unique names, so the seeded member stays as it is for the other projects.
import { expect, functionalProject, openSection, test, unique, writesTo } from "./fixtures.mjs";

const pane = (page) => page.locator(".admin-member");
const asked = (page) => page.getByRole("alert", { name: "Confirm action" });
const members = (page) => page.getByRole("table", { name: "Members" });
const row = (page, name) => members(page).locator("tbody tr").filter({ hasText: name });
// The page notice: under the area control, or in the open member's card after a write on it.
const notice = (page) => page.locator("main p.notice[role=status]");
const area = (page, label) => page.getByRole("group", { name: "Area" }).getByRole("button", { name: label, exact: true });
// A retained audit row is a card headed by its action, with the daemon's fields as a definition list.
const fact = (card, name) => card.locator("dt").filter({ hasText: new RegExp(`^${name}$`) }).locator("+ dd");
const loginOf = (display) => `${display.replaceAll(" ", "-").toLowerCase()}@example.test`;

async function addMember(page, display) {
  await page.locator("header").getByRole("button", { name: "Add member" }).click();
  await pane(page).getByLabel(/Exact Tailscale login/).fill(loginOf(display));
  await pane(page).getByLabel(/Display name/).fill(display);
  await pane(page).getByRole("button", { name: "Add member" }).click();
  await expect(notice(page)).toHaveText("Daemon accepted add member.");
  await expect(page.getByRole("heading", { name: display, level: 2 })).toBeVisible();
  return pane(page).getAttribute("data-member-id");
}

async function readAudit(page) {
  await area(page, "Audit").click();
  await page.locator(".admin-form").getByRole("button").click();
  await expect(page.locator(".admin-output").getByRole("heading", { name: "Read retained audit" })).toBeVisible();
  return page.locator(".admin-output section.admin-row");
}

test("the members table and the first member load without a run step, in readable values", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "admin");
  const seeded = row(page, "Synthetic member");
  for (const text of ["member@example.test", "Enabled", "0 B", "of 20 GiB", "Not linked"]) await expect(seeded).toContainText(text);
  await expect(page.getByRole("heading", { name: /^Members/ })).toContainText(/\d/);
  await expect(pane(page)).toContainText("0 B of 20 GiB");
  await expect(pane(page)).toContainText("1 running · 2 queued");
  await expect(pane(page)).toContainText("None yet");
  // No epoch seconds or raw booleans: the daemon's created_at is a float such as 1792187816.15. The member id is
  // random hex, which can hold ten digits in a row, so it is left out of the check.
  const id = await pane(page).getAttribute("data-member-id");
  expect((await pane(page).textContent()).replaceAll(id, "")).not.toMatch(/\d{10}/);
  await expect(pane(page)).not.toContainText("true");
  await expect(page.getByLabel("Choose action", { exact: true })).toHaveCount(0);
  await expect(page.locator(".admin-footnote")).toContainText(/^Not offered here: .+ On the daemon host: harness /);
  expect(errors).toEqual([]);
});

test("Add member sends only what was typed; a login already in use is refused, and the audit shows when and why (#88)",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    const display = unique(testInfo, "Added");
    await openSection(page, "admin");
    const added = writesTo(page, "/api/manage/account_add");
    // Add member is the page's primary action from any area, and it opens Accounts with the login field focused.
    await area(page, "Maintenance").click();
    await expect(notice(page)).toHaveText("Current daemon state.");
    await page.locator("header").getByRole("button", { name: "Add member" }).click();
    await expect(area(page, "Accounts")).toHaveAttribute("aria-pressed", "true");
    await expect(pane(page).getByLabel(/Exact Tailscale login/)).toBeFocused();
    const userId = await addMember(page, display);
    expect(userId).toMatch(/^u-[\w-]+$/);
    expect(added).toEqual([{ login: loginOf(display), display_name: display }]);
    await expect(members(page).locator("tr.selected")).toContainText(loginOf(display));

    // The same login again: the daemon refuses it, and records the refusal with its reason.
    await page.locator("header").getByRole("button", { name: "Add member" }).click();
    await pane(page).getByLabel(/Exact Tailscale login/).fill(loginOf(display));
    await pane(page).getByLabel(/Display name/).fill(`${display} again`);
    await pane(page).getByRole("button", { name: "Add member" }).click();
    await expect(notice(page)).toContainText("A household account already uses that login.");
    await expect(row(page, `${display} again`)).toHaveCount(0);

    // The retained audit holds the daemon's own rows: a readable time from ts and the free-text detail (#88).
    const rows = await readAudit(page);
    const refused = rows.filter({ has: page.getByRole("heading", { name: "create", exact: true }) })
      .filter({ has: page.locator("dd", { hasText: /^login already a member$/ }) }).first();
    await expect(fact(refused, "outcome")).toHaveText("denied");
    await expect(fact(refused, "detail")).toHaveText("login already a member");
    const created = rows.filter({ has: page.locator("dd", { hasText: new RegExp(`^${userId}$`) }) })
      .filter({ has: page.getByRole("heading", { name: "create", exact: true }) });
    await expect(fact(created, "outcome")).toHaveText("ok");
    for (const card of [refused, created]) {
      // when(): the browser's locale date and time, never the epoch seconds the daemon sent.
      await expect(fact(card, "ts")).toHaveText(/\d{1,4}\D\d{1,2}\D\d{1,4}.*\d{1,2}:\d{2}/);
      await expect(fact(card, "ts")).not.toHaveText(/\d{9}/);
    }
    expect(errors.filter((e) => !/status of 409/.test(e))).toEqual([]);
  });

test("Disable asks in the pane with the change and the member id; Cancel sends nothing; the notice stays with its member",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    const display = unique(testInfo, "Member");
    await openSection(page, "admin");
    await expect(members(page)).toContainText("GiB");
    const userId = await addMember(page, display);
    await page.reload();
    await expect(row(page, display)).toContainText("Enabled");
    await row(page, display).getByRole("button", { name: `Open ${display}` }).click();
    await expect(page.getByRole("heading", { name: display, level: 2 })).toBeVisible();
    const saved = writesTo(page, "/api/manage/account_save");

    await pane(page).getByRole("button", { name: "Disable", exact: true }).click();
    await expect(asked(page)).toContainText(userId);
    await expect(asked(page)).toContainText("Enabled → Disabled");
    await asked(page).getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(asked(page)).toHaveCount(0);
    expect(saved).toEqual([]);
    await pane(page).getByRole("button", { name: "Disable", exact: true }).click();
    await page.getByRole("button", { name: "Confirm update member", exact: true }).click();
    // The write's notice sits in the card it acted on; another member's card holds none, and it comes back with its own.
    await expect(pane(page).locator("p.notice[role=status]")).toHaveText("Daemon accepted update member.");
    expect(saved).toEqual([{ enabled: false, confirm: true }]);
    await expect(pane(page).getByRole("button", { name: "Enable", exact: true })).toBeEnabled();
    await expect(row(page, display)).toContainText("Disabled");
    await row(page, "Synthetic member").getByRole("button", { name: "Open Synthetic member" }).click();
    await expect(page.getByRole("heading", { name: "Synthetic member", level: 2 })).toBeVisible();
    await expect(pane(page).locator("p.notice")).toHaveCount(0);
    await expect(page.locator("main > p.notice[role=status]")).toHaveText("Daemon accepted update member.");
    await row(page, display).getByRole("button", { name: `Open ${display}` }).click();
    await expect(pane(page).locator("p.notice[role=status]")).toHaveText("Daemon accepted update member.");

    await page.reload();
    await expect(row(page, display)).toContainText("Disabled");
    await row(page, display).getByRole("button", { name: `Open ${display}` }).click();
    await expect(pane(page)).toContainText(userId);
    await expect(pane(page).getByRole("button", { name: "Enable", exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  });

test("Edit asks in the pane and sends only the changed fields, with byte counts the rounded size hides",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    const display = unique(testInfo, "Edited");
    await openSection(page, "admin");
    const userId = await addMember(page, display);
    const saved = writesTo(page, "/api/manage/account_save");
    await pane(page).getByRole("button", { name: "Edit member" }).click();
    await expect(pane(page).getByLabel("Display name")).toHaveValue(display);
    await expect(pane(page).getByLabel("Disk quota in bytes")).toHaveValue(String(20 * 1024 ** 3));
    await pane(page).getByRole("button", { name: "Save changes" }).click();
    await expect(notice(page)).toContainText("Nothing to save");
    await pane(page).getByLabel("Display name").fill(`${display} renamed`);
    await pane(page).getByLabel("Disk quota in bytes").fill(String(12 * 1024 ** 3));
    await expect(pane(page)).toContainText("That is 12 GiB.");
    await pane(page).getByRole("button", { name: "Save changes" }).click();
    await expect(asked(page)).toContainText(`${display} → ${display} renamed`);
    await expect(asked(page)).toContainText("20 GiB (21,474,836,480 bytes) → 12 GiB (12,884,901,888 bytes)");
    expect(saved).toEqual([]);
    await page.getByRole("button", { name: "Confirm update member", exact: true }).click();
    await expect(page.getByRole("heading", { name: `${display} renamed`, level: 2 })).toBeVisible();
    await expect(pane(page)).toContainText("0 B of 12 GiB");
    expect(saved).toEqual([{ display_name: `${display} renamed`, disk_quota_bytes: 12 * 1024 ** 3, confirm: true }]);

    // 400 MiB less still reads as 12 GiB, so the question names the exact bytes on both sides.
    await pane(page).getByRole("button", { name: "Edit member" }).click();
    await pane(page).getByLabel("Disk quota in bytes").fill(String(12 * 1024 ** 3 - 400 * 1024 ** 2));
    await pane(page).getByRole("button", { name: "Save changes" }).click();
    await expect(asked(page)).toContainText("12 GiB (12,884,901,888 bytes) → 12 GiB (12,465,471,488 bytes)");
    await asked(page).getByRole("button", { name: "Cancel", exact: true }).click();
    expect(saved).toHaveLength(1);
    await page.reload();
    await row(page, `${display} renamed`).getByRole("button", { name: `Open ${display} renamed` }).click();
    await expect(pane(page)).toHaveAttribute("data-member-id", userId);
    await expect(pane(page)).toContainText("0 B of 12 GiB");
    expect(errors).toEqual([]);
  });

test("Erase GitHub credential asks inside the danger zone; this host has no member GitHub sign-in, so the daemon refuses",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    await openSection(page, "admin");
    await addMember(page, unique(testInfo, "GitHub"));
    const zone = pane(page).locator(".danger-zone");
    await zone.getByRole("button", { name: "Erase GitHub credential" }).click();
    await expect(zone.getByRole("alert", { name: "Confirm action" })).toContainText("Erase this member's GitHub credential?");
    await expect(zone.getByRole("button", { name: "Confirm erase GitHub credential" })).toHaveClass(/danger/);
    await zone.getByRole("button", { name: "Confirm erase GitHub credential" }).click();
    await expect(notice(page)).toContainText("GitHub sign-in for household members is not set up on this machine.");
    expect(errors.filter((e) => !/status of 409/.test(e))).toEqual([]);
  });

test("a Google link code shows once and is cleared by another member, the Audit area, Cancel and leaving the page",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    const display = unique(testInfo, "Google");
    await openSection(page, "admin");
    await addMember(page, display);
    const code = page.locator("[data-link-code]");
    const create = async () => {
      await pane(page).getByRole("button", { name: "Create link code" }).click();
      await expect(code).toHaveText(/^ahg-[\w-]+$/);
      return code.innerText();
    };
    const first = await create();
    await expect(pane(page).getByRole("button", { name: "Dismiss code" })).toBeFocused();
    await expect(pane(page)).toContainText("link code open until");
    await row(page, "Synthetic member").getByRole("button", { name: "Open Synthetic member" }).click();
    await expect(page.getByText(first)).toHaveCount(0);
    await row(page, display).getByRole("button", { name: `Open ${display}` }).click();
    const second = await create();
    await area(page, "Audit").click();
    await expect(page.getByText(second)).toHaveCount(0);
    await area(page, "Accounts").click();
    await row(page, display).getByRole("button", { name: `Open ${display}` }).click();
    const third = await create();
    await pane(page).getByRole("button", { name: "Cancel link code" }).click();
    await page.getByRole("button", { name: "Confirm cancel Google link code", exact: true }).click();
    await expect(page.getByText(third)).toHaveCount(0);
    await expect(pane(page)).not.toContainText("link code open until");
    const fourth = await create();
    await page.goto(`${process.env.HUB_UI_URL}/#/overview`);
    await page.goBack();
    await expect(pane(page)).toBeVisible();
    await expect(page.getByText(fourth)).toHaveCount(0);
    expect(errors).toEqual([]);
  });

test("Clean up asks first and reports the daemon's answer", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "admin");
  const cleanups = writesTo(page, "/api/manage/cleanup");
  await area(page, "Maintenance").click();
  await expect(notice(page)).toHaveText("Current daemon state.");
  await page.getByLabel("Choose action", { exact: true }).selectOption("cleanup");
  await page.locator(".admin-form").getByRole("button").click();
  await expect(asked(page)).toBeVisible();
  expect(cleanups).toEqual([]);
  await page.getByRole("button", { name: "Confirm clean up" }).click();
  await expect(notice(page)).toHaveText("Daemon accepted clean up. Read current state before another change.");
  expect(cleanups).toHaveLength(1);
  expect(errors).toEqual([]);
});

for (const scheme of ["light", "dark"]) {
  test(`the chosen area segment looks selected in the ${scheme} theme`, async ({ signedIn: page, errors }, testInfo) => {
    test.skip(testInfo.project.name !== `desktop-${scheme}`, "one desktop project per scheme");
    await openSection(page, "admin");
    await area(page, "Audit").click();
    await expect(page.getByRole("group", { name: "Area" }).locator('[aria-pressed="true"]')).toHaveText("Audit");
    // The chosen segment's outline stands out from the track at least 3:1 (non-text contrast).
    const ratio = await page.evaluate(() => {
      const rgb = (colour) => colour.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
      const lum = ([r, g, b]) => [r, g, b].map((v) => v / 255).map((v) => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
      const track = getComputedStyle(document.querySelector(".segmented")).backgroundColor;
      const outline = getComputedStyle(document.querySelector('.segmented [aria-pressed="true"]')).borderTopColor;
      const [a, b] = [outline, track].map((colour) => lum(rgb(colour)));
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
    expect(ratio).toBeGreaterThanOrEqual(3);
    expect(errors).toEqual([]);
  });
}
