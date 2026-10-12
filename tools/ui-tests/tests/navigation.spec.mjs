// HUB-01 navigation and layout: every section loads and survives a reload, the theme follows the color scheme, and no
// page scrolls sideways at 320, 375, 768 or 1440 px.
import { SECTIONS, expect, expectNoHorizontalOverflow, hubUrl, isPhone, openSection, scheme, test } from "./fixtures.mjs";

const BACKGROUND = { light: "#f3f4f6", dark: "#000000" };

test("each section loads, is marked current and survives a reload", async ({ signedIn: page, errors }, testInfo) => {
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()))
    .toBe(BACKGROUND[scheme(testInfo)]);
  for (const section of SECTIONS) {
    await openSection(page, section.id);
    await expect(page).toHaveTitle(`${section.label} · Hub`);
    await expect(page.locator(`#nav a[data-section="${section.id}"]`)).toHaveAttribute("aria-current", "page");
    await page.reload();
    await expect(section.ready(page).first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("no horizontal overflow at 320, 375, 768 and 1440 px", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(isPhone(testInfo), "breakpoints are swept from the desktop projects; phones check their own width");
  for (const section of SECTIONS) {
    await openSection(page, section.id);
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => window.innerWidth)).toBe(width);
      await expectNoHorizontalOverflow(page);
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  }
  expect(errors).toEqual([]);
});

test("phone: pages fit the device width and the drawer navigates", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(!isPhone(testInfo), "phone projects only");
  for (const section of SECTIONS) {
    await openSection(page, section.id);
    await expectNoHorizontalOverflow(page);
  }
  const menu = page.getByRole("button", { name: "Sections" });
  await page.goto(`${hubUrl()}/#/overview`);
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await menu.click();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("link", { name: "Apps" }).click();
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("heading", { name: "Apps", level: 1 })).toBeVisible();
  await menu.click();
  await expect(page.locator('#nav a[data-section="apps"]')).toHaveAttribute("aria-current", "page");
  await page.keyboard.press("Escape");
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  expect(errors).toEqual([]);
});

test("live resize keeps a partly filled form and a working menu", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(isPhone(testInfo), "desktop projects resize; phones have a fixed device viewport");
  await openSection(page, "settings");
  const turns = page.getByRole("spinbutton", { name: "Maximum turns" });
  const original = await turns.inputValue();
  await turns.fill(String(Number(original) + 7));
  const menu = page.getByRole("button", { name: "Sections" });
  for (const width of [1280, 1024, 768, 414, 375]) await page.setViewportSize({ width, height: 900 });
  await expect(menu).toBeVisible();
  await menu.click();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("link", { name: "Modules" })).toBeVisible();
  for (const width of [768, 1024, 1440]) await page.setViewportSize({ width, height: 900 });
  await expect(menu).toBeHidden();
  await expect(turns).toHaveValue(String(Number(original) + 7));
  await expectNoHorizontalOverflow(page);
  // The rail works and nothing left from the drawer covers the page.
  await turns.click();
  await expect(turns).toBeFocused();
  await page.getByRole("link", { name: "Modules" }).click();
  await expect(page.getByRole("heading", { name: "Modules", level: 1 })).toBeVisible();
  expect(errors).toEqual([]);
});
