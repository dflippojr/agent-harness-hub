// HUB-05 module switches and HUB-06 app management against the real daemon's inventory and key store.
import { expect, functionalProject, openSection, pairApp, test, writesTo } from "./fixtures.mjs";

test("a module switch saves, survives a reload and switches back", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "modules");
  // A healthy inventory shows no empty error box.
  await expect(page.locator("tr[data-module=hub]")).toContainText("Running");
  await expect(page.locator(".notice.bad:visible")).toHaveCount(0);
  for (const alert of await page.locator("[role=alert]:visible").all()) await expect(alert).not.toHaveText("");
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

test("app session caps and retention save after confirmation and persist", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "apps");
  const row = page.locator("tr[data-app-id]").filter({ hasText: "Synthetic App" });
  await expect(row).toContainText("App-supplied name");
  await expect(row).toContainText("Never used");
  // A healthy inventory shows no empty error box.
  await expect(page.locator(".notice.bad:visible")).toHaveCount(0);
  for (const alert of await page.locator("[role=alert]:visible").all()) await expect(alert).not.toHaveText("");
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
  await expect(retention).toBeDisabled();
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(detail.getByRole("button", { name: "Save retention" })).toBeFocused();
  await expect(retention).toHaveValue(days);
  await detail.getByRole("button", { name: "Save retention" }).click();
  // The question locks the field it applies, also across a Refresh.
  await expect(retention).toBeDisabled();
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await expect(confirm).toBeVisible();
  await expect(retention).toBeDisabled();
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

test("an unchanged form sends nothing, and a half-typed cap stays invalid across a redraw", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "apps");
  const row = page.locator("tr[data-app-id]").filter({ hasText: "Synthetic App" });
  const id = await row.getAttribute("data-app-id");
  const caps = writesTo(page, `/api/apps/${id}/limits`);
  await row.getByRole("button", { name: "Open Synthetic App" }).click();
  const detail = page.locator(`[data-detail-id="${id}"]`);
  const running = detail.getByLabel(/^Maximum running/);
  await expect(running).toBeVisible();
  await detail.getByRole("button", { name: "Save session caps" }).click();
  await expect(page.getByText("Nothing to save: both caps are as the daemon has them.", { exact: true })).toBeVisible();
  await running.fill("");
  await running.pressSequentially("1e");
  // Asking the retention question redraws the detail; the half-typed cap keeps its own element and stays invalid.
  await detail.getByLabel("Retention days").fill("5");
  await detail.getByRole("button", { name: "Save retention" }).click();
  await detail.getByRole("button", { name: "Cancel" }).click();
  await expect.poll(() => running.evaluate((input) => input.validity.badInput)).toBe(true);
  await detail.getByRole("button", { name: "Save session caps" }).click();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve))));
  expect(caps).toEqual([]);
  expect(errors).toEqual([]);
});

test("a freshly paired App: Revoke asks in its danger zone, Cancel sends nothing, and Confirm queues its erasure",
  async ({ signedIn: page, request, errors }, testInfo) => {
    functionalProject(testInfo);
    // Its own key, paired through an owner-armed slot, so the seeded App stays for the other journeys.
    const key = await pairApp(page, request, "Agent Harness Web");
    await openSection(page, "apps");
    const row = page.locator(`tr[data-app-id="${key.id}"]`);
    await expect(row.locator(".app-status")).toContainText("Never used");
    await row.getByRole("button").click();
    const detail = page.locator(`[data-detail-id="${key.id}"]`);
    const revoke = detail.getByRole("button", { name: /^Revoke/ });
    const revokes = writesTo(page, `/api/apps/${key.id}/revoke`);
    await revoke.click();
    const ask = detail.locator(".danger-zone [role=alert]");
    await expect(ask.getByRole("heading", { name: /^Revoke/ })).toBeVisible();
    // The question names the actual key, not only the name the App chose.
    for (const text of [key.id, "Catalog app", "ahpub.dflippojr.agent-harness-web", "Created"]) await expect(ask).toContainText(text);
    await ask.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(revoke).toBeFocused();
    expect(revokes).toEqual([]);
    await revoke.click();
    await detail.getByRole("button", { name: /^Confirm revoke/ }).click();
    // Restoring is host-only: the command shows in the detail and on the row, and the Hub offers no Revoke again.
    await expect(detail).toContainText("Revoked · erases");
    await expect(detail.getByText(`harness apps restore ${key.id}`, { exact: true })).toBeVisible();
    await expect(detail.getByRole("button", { name: /^Revoke/ })).toHaveCount(0);
    expect(revokes).toEqual([{ confirm: true }]);
    await page.reload();
    await expect(row.locator(".app-status")).toContainText("Revoked · erases");
    await expect(row.getByRole("button", { name: "Restore command for Agent Harness Web" })).toBeVisible();
    expect(errors).toEqual([]);
  });

test("this Hub's own key is listed with its release command and offers no App controls", async ({ signedIn: page, errors }, testInfo) => {
  functionalProject(testInfo);
  await openSection(page, "apps");
  // The Hub's own key reads "This Hub", the label the reserved-name check keeps for it alone.
  const row = page.locator("tr[data-app-id]").filter({ has: page.getByRole("button", { name: "Open This Hub" }) });
  await expect(row).not.toContainText("Reserved name");
  const id = await row.getAttribute("data-app-id");
  await row.getByRole("button").click();
  const detail = page.locator(`[data-detail-id="${id}"]`);
  await expect(detail).toContainText("harness hub release --confirm");
  await expect(detail).not.toContainText("reserved This Hub label");
  await expect(detail.getByRole("button", { name: /^(Revoke|Save|Confirm)/ })).toHaveCount(0);
  await expect(detail.getByText(/harness apps restore/)).toHaveCount(0);
  expect(errors).toEqual([]);
});
