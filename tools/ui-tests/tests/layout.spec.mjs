// Layout over the real daemon's rows: 44 px targets and no sideways scroll on the pages with the densest cards, at a
// phone and a desktop width. One Chromium desktop project resizes; navigation.spec.mjs sweeps every section's overflow
// on every device.
import { appClaims, armSlot, expect, hubApi, openSection, pkce, test, unique } from "./fixtures.mjs";

const desktopLight = (testInfo) => test.skip(testInfo.project.name !== "desktop-light", "resizes one desktop project");
const WIDTHS = [390, 1280];

// Every rendered target in `scope` is at least 44 px each way, with Host commands shown and then hidden.
async function expectTargets(page, scope, selector = "button:visible, input:visible, summary:visible") {
  for (const shown of [true, false]) {
    await page.evaluate((on) => document.body.classList.toggle("show-host-commands", on), shown);
    for (const target of await scope.locator(selector).all()) {
      const box = await target.boundingBox();
      const label = await target.evaluate((el) => el.outerHTML.slice(0, 80));
      expect(box.height, label).toBeGreaterThanOrEqual(44);
      expect(box.width, label).toBeGreaterThanOrEqual(44);
    }
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

test("Pairing requests and Marketplace keep 44 px targets and no sideways scroll", async ({ signedIn: page, request, errors }, testInfo) => {
  desktopLight(testInfo);
  // A claimed and an armed slot beside whatever the instance holds (the seeded requests, other journeys' History).
  const claimed = await armSlot(page);
  await armSlot(page);
  await appClaims(request, claimed.id, pkce().challenge);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await openSection(page, "pairing");
    await expectTargets(page, page.getByRole("main"), "main button:visible, main input:visible");
    await openSection(page, "marketplace");
    await page.getByRole("button", { name: "Set up financial-planner" }).click();
    await expect(page.locator(".market-consent")).toBeVisible();
    await expectTargets(page, page.getByRole("main"), "main button:visible, main input:visible, main summary:visible");
  }
  const { json } = await hubApi(page, "marketplace");
  for (const slot of json.entries.flatMap((e) => e.slots)) {
    await hubApi(page, `pairing/${slot.id}/deny`, { method: "POST", body: { confirm: true } });
  }
  expect(errors).toEqual([]);
});

test("Accounts keeps 44 px targets, and a long login at 320 px stays inside the members table", async ({ signedIn: page, errors }, testInfo) => {
  desktopLight(testInfo);
  const display = unique(testInfo, "Wide");
  await openSection(page, "admin");
  await page.locator("header").getByRole("button", { name: "Add member" }).click();
  const pane = page.locator(".admin-member");
  await pane.getByLabel(/Exact Tailscale login/).fill(`householdmemberwithalonglogin-${Date.now().toString(36)}@example.test`);
  await pane.getByLabel(/Display name/).fill(display);
  await pane.getByRole("button", { name: "Add member" }).click();
  await expect(page.getByRole("heading", { name: display, level: 2 })).toBeVisible();
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await expect(pane).toContainText("of 20 GiB");
    await expectTargets(page, page.getByRole("main"), "main button:visible, main input:visible, main select:visible, main textarea:visible");
  }
  await page.setViewportSize({ width: 320, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  const open = await page.getByRole("button", { name: `Open ${display}` }).boundingBox();
  expect(open.x + open.width).toBeLessThanOrEqual(320);
  expect(errors).toEqual([]);
});

test("Apps puts the detail beside the list from 1200 px, in its place below, with 44 px targets", async ({ signedIn: page, errors }, testInfo) => {
  desktopLight(testInfo);
  await openSection(page, "apps");
  const row = page.locator("tr[data-app-id]").filter({ hasText: "Synthetic App" });
  const id = await row.getAttribute("data-app-id");
  await row.getByRole("button", { name: "Open Synthetic App" }).click();
  const detail = page.locator(`[data-detail-id="${id}"]`);
  await expect(detail.getByRole("heading", { level: 2, name: "Synthetic App" })).toBeFocused();
  await expect(row).toHaveClass(/selected/);
  // Once the caps have loaded (a redraw), the heading keeps focus and the list sits beside the detail.
  await expect(detail.getByLabel(/^Maximum running/)).toBeVisible();
  await expect(detail.getByRole("heading", { level: 2, name: "Synthetic App" })).toBeFocused();
  const [list, pane] = [await page.locator(".apps-keys").boundingBox(), await detail.boundingBox()];
  expect(list.x + list.width).toBeLessThanOrEqual(pane.x);
  await expect(detail.getByLabel("Retention days")).toHaveAttribute("placeholder", "Keep until deleted");

  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(page.locator(".apps-keys")).toBeHidden();
  await detail.getByRole("button", { name: "Back to all apps" }).click();
  await expect(detail).toHaveCount(0);
  await expect(row.getByRole("button", { name: "Open Synthetic App" })).toBeFocused();
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    if (width === 390) await row.getByRole("button", { name: "Open Synthetic App" }).click();
    await expectTargets(page, page.getByRole("main"), "main button:visible, main input:visible, main a:visible");
  }
  expect(errors).toEqual([]);
});
