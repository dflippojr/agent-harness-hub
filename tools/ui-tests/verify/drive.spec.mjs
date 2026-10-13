// The verify skill's main path (.claude/skills/verify/SKILL.md, "Feature map"): sign in by link, open Overview, Apps,
// Modules and Pairing requests, then one Settings edit rolled back. Each test is tagged with its feature, so
// `--grep @settings` drives just that area. Screenshots go to VERIFY_EVIDENCE_DIR/screenshots.
import path from "node:path";
// Sign-in happens in each test body, after the `errors` fixture is listening, so startup errors count too.
import { expect, openSection, signIn, test } from "../tests/fixtures.mjs";

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

test("edit one setting and roll it back @settings", async ({ page, errors }, testInfo) => {
  await signIn(page);
  await openSection(page, "settings");
  const turns = page.getByRole("spinbutton", { name: "Maximum turns" });
  const status = page.locator("main p.notice[aria-live]").first();
  const original = Number(await turns.inputValue());

  await turns.fill(String(original + 1));
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(status).toContainText("Validated.");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(status).toContainText("Saved.");
  await page.reload();
  await expect(turns).toHaveValue(String(original + 1));
  await turns.scrollIntoViewIfNeeded();
  await capture(page, testInfo, "settings-edited");

  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "Roll back settings?" })
    .getByRole("button", { name: "Confirm rollback" }).click();
  await expect(status).toContainText("Rolled back to revision");
  await page.reload();
  await expect(turns).toHaveValue(String(original));
  await turns.scrollIntoViewIfNeeded();
  await capture(page, testInfo, "settings");
  expect(errors).toEqual([]);
});
