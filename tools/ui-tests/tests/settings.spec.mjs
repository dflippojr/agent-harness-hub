// HUB-04 settings: validation errors save nothing; a saved edit persists across a reload; rollback asks first, Cancel
// keeps the edit, and confirming restores the previous value.
import { expect, openSection, test } from "./fixtures.mjs";

test("validate, save, reload, then roll back with confirmation", async ({ signedIn: page, errors }) => {
  await openSection(page, "settings");
  const turns = page.getByRole("spinbutton", { name: "Maximum turns" });
  const status = page.locator("main p.notice[aria-live]").first();
  const original = Number(await turns.inputValue());

  await turns.fill("-5");
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(page.getByText("must be >= 1")).toBeVisible();
  await page.reload();
  await expect(turns).toHaveValue(String(original));

  const edited = original + 1;
  await turns.fill(String(edited));
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(status).toContainText(`sessions.max_turns: ${original} → ${edited}`);
  await expect(status).toContainText("Validated.");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(status).toContainText("Saved.");
  await page.reload();
  await expect(turns).toHaveValue(String(edited));

  const rollback = page.getByRole("alert").filter({ hasText: "Roll back settings?" });
  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await expect(rollback).toContainText("Unsaved edits will be discarded.");
  await rollback.getByRole("button", { name: "Cancel" }).click();
  await expect(rollback).toHaveCount(0);
  await expect(turns).toHaveValue(String(edited));

  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await rollback.getByRole("button", { name: "Confirm rollback" }).click();
  await expect(status).toContainText("Rolled back to revision");
  await page.reload();
  await expect(turns).toHaveValue(String(original));
  // The refused validation (-5) is a 400 the browser logs as a failed resource load.
  expect(errors.filter((e) => !/status of 400/.test(e))).toEqual([]);
});
