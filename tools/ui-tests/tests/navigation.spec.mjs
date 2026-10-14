// HUB-01 navigation and layout: every section loads and survives a reload, the theme follows the color scheme, the
// rail is grouped with its footer, and no page scrolls sideways at 320, 375, 768 or 1440 px.
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

test("the rail groups the sections, shows the daemon chip and banner, and remembers the host-commands switch", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(isPhone(testInfo), "the rail is a drawer on phones; the phone test covers it");
  const nav = page.getByRole("navigation", { name: "Sections" });
  await expect(page.locator(".topbar")).toBeHidden();
  await expect(nav.getByRole("list", { name: "Access" }).getByRole("link"))
    .toHaveText([/^Pairing requests/, "Apps", "Spend", "Marketplace"]);
  await expect(nav.getByRole("list", { name: "Daemon" }).getByRole("link"))
    .toHaveText(["Settings", "Modules", "Accounts and maintenance"]);
  // The pinned daemon (aa5a904) reports Owner API 1.24: live, but below the 1.26 tailnet sign-in needs.
  await expect(nav.getByRole("status")).toHaveText("Daemon live · API 1.24");
  const slot = page.locator("main > .page-head + #banner-slot");
  await expect(slot.getByRole("status")).toContainText("Tailnet sign-in needs Owner API 1.26 or later.");
  // Overview's answer is held in memory: the banner stays under every page head without new reads.
  await nav.getByRole("link", { name: "Apps", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Apps", level: 1 })).toBeVisible();
  await expect(slot.getByRole("status")).toContainText("Tailnet sign-in needs Owner API 1.26 or later.");

  // Host commands (#70): off by default, each action's CLI equivalent waits behind its Host command button.
  const host = nav.getByRole("switch", { name: "Show host commands" });
  const keysList = page.getByText("harness keys list", { exact: true });
  const reveal = page.getByRole("main").getByRole("button", { name: "Host commands", exact: true }).first();
  await expect(host).not.toBeChecked();
  await expect(keysList).toBeHidden();
  await expect(reveal).toHaveAttribute("aria-expanded", "false");
  await reveal.click();
  await expect(reveal).toHaveAttribute("aria-expanded", "true");
  await expect(keysList).toBeVisible();
  await reveal.click();
  await expect(keysList).toBeHidden();
  // The switch shows every command in place of the buttons, and the browser remembers it.
  await host.check();
  await expect(page.locator("body")).toHaveClass(/show-host-commands/);
  await expect(keysList).toBeVisible();
  await expect(reveal).toBeHidden();
  await page.reload();
  await expect(host).toBeChecked();
  await expect(keysList).toBeVisible();
  await host.uncheck();
  await page.reload();
  await expect(host).not.toBeChecked();
  await expect(keysList).toBeHidden();
  expect(await nav.getByRole("button", { name: "Sign out" }).boundingBox()).toBeTruthy();
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
  // The drawer is the rail: the footer's switch and Sign out are in it.
  await expect(page.getByRole("switch", { name: "Show host commands" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  // A tap inside the drawer that is neither a link nor a footer control leaves it open (a group label).
  await menu.tap();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  await page.locator("#nav .nav-group").first().tap();
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  // A press on the dimmed backdrop beside the drawer closes it and returns focus to the menu button. (A synthesized
  // touch tap on the bare backdrop does not reach the page's click handler in Playwright's WebKit, so this clicks.)
  await page.locator("body").click({ position: { x: page.viewportSize().width - 12, y: 400 } });
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await expect(menu).toBeFocused();
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
