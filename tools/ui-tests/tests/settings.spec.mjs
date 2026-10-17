// HUB-04 settings: validation errors save nothing; a saved edit persists across a reload; rollback asks first, Cancel
// keeps the edit, and confirming restores the previous value. Hub design 8: one category at a time from the category
// list, the filter, the Changed pill, the save bar that shows only with edits, and Discard.
import { confirmRollback, expect, expectNoHorizontalOverflow, functionalProject, openSection, saveMaxTurns, settingsControls, test }
  from "./fixtures.mjs";

test("validate, save, reload, then roll back with confirmation", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "settings");
  const { turns, saveBar } = settingsControls(page);
  const original = Number(await turns.inputValue());

  await turns.fill("-5");
  await saveBar.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(page.getByText("must be >= 1")).toBeVisible();
  await page.reload();
  await expect(turns).toHaveValue(String(original));

  const edited = original + 1;
  await saveMaxTurns(page, original, edited);

  const rollback = page.getByRole("alert").filter({ hasText: "Roll back settings?" });
  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await expect(rollback).toContainText("Unsaved edits will be discarded.");
  await rollback.getByRole("button", { name: "Cancel" }).click();
  await expect(rollback).toHaveCount(0);
  await expect(turns).toHaveValue(String(edited));

  await confirmRollback(page, original);
  // The refused validation (-5) is a 400 the browser logs as a failed resource load.
  expect(errors.filter((e) => !/status of 400/.test(e))).toEqual([]);
});

test("categories, filter, Changed pill and Discard", async ({ signedIn: page, errors }) => {
  await openSection(page, "settings");
  const { turns, saveBar } = settingsControls(page);
  const original = await turns.inputValue();
  const categories = page.getByRole("navigation", { name: "Setting categories" });
  const sessions = categories.getByRole("button", { name: /^Sessions \d+$/ });
  await expect(sessions).toHaveAttribute("aria-current", "true");
  await expect(saveBar).toBeHidden();

  // An edit marks its row and opens the save bar; it survives a visit to another category.
  const row = page.locator(".setting-row").filter({ has: turns });
  await turns.fill(String(Number(original) + 2));
  await expect(row.getByText("Changed", { exact: true })).toBeVisible();
  await expect(saveBar).toContainText("1 unsaved change");
  await categories.getByRole("button", { name: /^Compaction/ }).click();
  await expect(page.getByRole("heading", { name: "Compaction", level: 2 })).toBeVisible();
  await expect(turns).toBeHidden();
  await expect(saveBar).toContainText("1 unsaved change");
  await sessions.click();
  await expect(turns).toHaveValue(String(Number(original) + 2));
  await saveBar.getByRole("button", { name: "Discard" }).click();
  await expect(turns).toHaveValue(original);
  await expect(row.getByText("Changed", { exact: true })).toBeHidden();
  await expect(saveBar).toBeHidden();

  // The filter narrows the rows and the list; a filter with no match says so and clears.
  const filter = page.getByRole("searchbox", { name: "Filter settings" });
  await filter.fill("completion tokens");
  await expect(sessions).toHaveText(/^Sessions 1$/);
  await expect(page.getByRole("spinbutton", { name: "Maximum completion tokens" })).toBeVisible();
  await expect(turns).toBeHidden();
  await filter.fill("no such setting anywhere");
  await expect(page.getByRole("heading", { name: "No settings match" })).toBeVisible();
  await page.getByRole("button", { name: "Clear filter" }).click();
  await expect(turns).toBeVisible();

  // The daemon's other controls are categories too.
  await categories.getByRole("button", { name: "Notifications" }).click();
  await expect(page.getByRole("button", { name: "Send test notification" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
  expect(errors).toEqual([]);
});

test("the absent memory library module is explained, not shown as an error", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  // The instance turns memory_library off, so the daemon answers 404 for its profile.
  await openSection(page, "settings");
  await page.getByRole("button", { name: "Memory agent profile" }).click();
  const box = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "Memory agent profile" }) });
  await expect(box).toContainText("The memory library module (memory_library) is not installed or is switched off");
  await expect(box.getByText("harness config set memory_library.enabled=true", { exact: true })).toBeVisible();
  await expect(box.getByLabel("Agent profile", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Not Found")).toHaveCount(0);
  await expect(box.locator(".notice.bad")).toHaveCount(0);
  expect(errors).toEqual([]);
});
