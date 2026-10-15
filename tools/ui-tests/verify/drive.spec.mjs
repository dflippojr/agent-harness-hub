// The verify skill's main path (.claude/skills/verify/SKILL.md, "Feature map"): sign in by link, open Overview, Apps,
// Modules and Pairing requests, read Accounts and maintenance, then one Settings edit rolled back. Each test is tagged with its feature, so
// `--grep @settings` drives just that area. Screenshots go to VERIFY_EVIDENCE_DIR/screenshots.
import path from "node:path";
// Sign-in happens in each test body, after the `errors` fixture is listening, so startup errors count too.
import { confirmRollback, expect, expectNoHorizontalOverflow, openSection, saveMaxTurns, settingsControls, signIn, test } from "../tests/fixtures.mjs";

const shots = path.join(process.env.VERIFY_EVIDENCE_DIR || "test-results/verify", "screenshots");

async function capture(page, testInfo, feature) {
  await page.screenshot({ path: path.join(shots, `${testInfo.project.name}-${feature}.png`) });
}

test("sign in by link @signin", async ({ page, errors }, testInfo) => {
  await signIn(page);
  await expect(page).toHaveURL(/#\/overview$/);
  await expect(page.getByRole("region", { name: "Sign in to the Hub" })).toHaveCount(0);
  await capture(page, testInfo, "signin");
  expect(errors).toEqual([]);
});

for (const feature of ["overview", "apps", "modules", "pairing"]) {
  test(`open ${feature} @${feature}`, async ({ page, errors }, testInfo) => {
    await signIn(page);
    await openSection(page, feature);
    await capture(page, testInfo, feature);
    expect(errors).toEqual([]);
  });
}

test("read Spend and sort its App table @spend", async ({ page, errors }, testInfo) => {
  await signIn(page);
  await openSection(page, "spend");
  const apps = page.getByRole("table", { name: "App usage, sortable by any column" });
  await expect(apps.getByRole("row", { name: /Synthetic App/ })).toBeVisible();
  await expect(apps.getByRole("columnheader", { name: /Cost/ })).toHaveAttribute("aria-sort", "descending");
  await apps.getByRole("button", { name: /^App/ }).click();
  await expect(apps.getByRole("columnheader", { name: /^App/ })).toHaveAttribute("aria-sort", "ascending");
  await expect(page.getByRole("table", { name: "Backend usage today and over the last 7 days" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "spend");
  expect(errors).toEqual([]);
});

test("read the members and open a member's edit form @admin", async ({ page, errors }, testInfo) => {
  await signIn(page);
  await openSection(page, "admin");
  const members = page.getByRole("table", { name: "Members" });
  const seeded = members.getByRole("row").filter({ hasText: "member@example.test" });
  await expect(seeded).toContainText("Enabled");
  await expect(seeded).toContainText("GiB");
  const pane = page.locator(".admin-member");
  await expect(pane.getByRole("heading", { name: "Synthetic member", level: 2 })).toBeVisible();
  await expect(pane.getByRole("button", { name: "Erase GitHub credential" })).toBeVisible();
  await expect(page.locator(".admin-footnote")).toContainText("harness keys create");
  await expectNoHorizontalOverflow(page);
  await capture(page, testInfo, "admin");
  // The edit form opens in the pane and Cancel leaves without a write.
  await pane.getByRole("button", { name: "Edit member" }).click();
  await expect(pane.getByLabel("Display name")).toHaveValue("Synthetic member");
  await pane.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(pane.getByRole("button", { name: "Edit member" })).toBeVisible();
  expect(errors).toEqual([]);
});

test("edit one setting and roll it back @settings", async ({ page, errors }, testInfo) => {
  await signIn(page);
  await openSection(page, "settings");
  const { turns } = settingsControls(page);
  const original = Number(await turns.inputValue());

  await saveMaxTurns(page, original, original + 1);
  await turns.scrollIntoViewIfNeeded();
  await capture(page, testInfo, "settings-edited");

  await confirmRollback(page, original);
  await turns.scrollIntoViewIfNeeded();
  await capture(page, testInfo, "settings");
  expect(errors).toEqual([]);
});
