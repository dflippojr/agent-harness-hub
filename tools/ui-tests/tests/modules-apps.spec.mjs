// HUB-05 module switches and HUB-06 app management against the real daemon's inventory and key store.
import { expect, openSection, test } from "./fixtures.mjs";

test("a module switch saves, survives a reload and switches back", async ({ signedIn: page, errors }) => {
  await openSection(page, "modules");
  const search = page.locator("tr[data-module=search]");
  const toggle = search.getByRole("switch", { name: "search", exact: true });
  await expect(toggle).not.toBeChecked();
  await expect(search.locator(".modules-status")).toHaveText("Off");
  await expect(search.locator(".modules-applies")).toContainText("Restart");
  await toggle.click();
  await expect(page.locator("main p.notice[aria-live]").first()).toContainText("Saved search.enabled=true");
  await expect(search).toContainText("On after restart");
  const restart = page.locator(".banner").filter({ hasText: "Restart needed to apply saved settings." });
  await expect(restart.getByRole("link", { name: "Restart in Settings" })).toBeVisible();
  await page.reload();
  await expect(toggle).toBeChecked();
  await expect(search).toContainText("On after restart");

  await toggle.click();
  await expect(page.locator("main p.notice[aria-live]").first()).toContainText("Saved search.enabled=false");
  await page.reload();
  await expect(toggle).not.toBeChecked();
  expect(errors).toEqual([]);
});

test("app session caps and retention save after confirmation and persist", async ({ signedIn: page, errors }) => {
  await openSection(page, "apps");
  const row = page.locator("tr[data-app-id]").filter({ hasText: "Synthetic App" });
  await expect(row).toContainText("App-supplied name");
  await expect(row).toContainText("Never used");
  const detail = page.locator("section.apps-detail");
  const open = async () => {
    await row.getByRole("button", { name: "Open Synthetic App" }).click();
    await expect(detail.getByRole("heading", { name: "Synthetic App", level: 2 })).toBeFocused();
  };
  await open();
  const running = detail.getByLabel(/^Maximum running/);
  await expect(running).toHaveAttribute("placeholder", /^(Default \d+|Daemon default)$/);
  // A value other than the current one (the projects share one instance): a save sends only a changed cap.
  const value = String((Number(await running.inputValue()) || 1) % 9 + 2);
  await running.fill(value);
  await detail.getByRole("button", { name: "Save session caps" }).click();
  await expect(page.locator("main p.notice[aria-live]").first()).toHaveText("Session caps saved.");

  await page.reload();
  await open();
  await expect(running).toHaveValue(value);
  await expect(detail).toContainText(`Effective caps: running ${value}`);

  // Retention asks inside its own card; Cancel saves nothing and keeps the typed value.
  const retention = detail.getByLabel("Retention days");
  const days = String(20 + (Date.now() % 50));
  await retention.fill(days);
  await detail.getByRole("button", { name: "Save retention" }).click();
  const confirm = detail.getByRole("alert").filter({ hasText: "Change retention for Synthetic App?" });
  await expect(confirm).toContainText(`Apply retention ${days} days to Synthetic App.`);
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(retention).toHaveValue(days);
  await detail.getByRole("button", { name: "Save retention" }).click();
  await confirm.getByRole("button", { name: "Confirm change retention for Synthetic App" }).click();
  await expect(page.locator("main p.notice[aria-live]").first()).toHaveText("Retention saved.");
  await page.reload();
  await open();
  await expect(retention).toHaveValue(days);

  // The daemon's retained audit records both changes.
  await openSection(page, "admin");
  await page.getByRole("button", { name: "Audit", exact: true }).click();
  await page.locator(".admin-form").getByRole("button").click();
  await expect(page.locator(".admin-output")).toContainText("app.limits");
  await expect(page.locator(".admin-output")).toContainText("app.retention");
  expect(errors).toEqual([]);
});
