// HUB-05 module switches and HUB-06 app management against the real daemon's inventory and key store.
import { expect, openSection, test } from "./fixtures.mjs";

test("a module switch saves, survives a reload and switches back", async ({ signedIn: page, errors }) => {
  await openSection(page, "modules");
  const search = page.locator("section").filter({ has: page.getByRole("heading", { name: "search", exact: true }) });
  await expect(search).toContainText("Switch: off");
  await search.getByRole("button", { name: "Switch search on" }).click();
  await expect(page.locator("main p.notice[aria-live]").first()).toContainText("Saved search.enabled=true");
  await expect(page.getByText("Restart needed to apply saved switches.")).toBeVisible();
  await page.reload();
  await expect(search.getByRole("button", { name: "Switch search off" })).toBeVisible();
  await expect(search).toContainText("Switch: on");

  await search.getByRole("button", { name: "Switch search off" }).click();
  await expect(page.locator("main p.notice[aria-live]").first()).toContainText("Saved search.enabled=false");
  await page.reload();
  await expect(search.getByRole("button", { name: "Switch search on" })).toBeVisible();
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
  const value = String(2 + (Date.now() % 9));
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
