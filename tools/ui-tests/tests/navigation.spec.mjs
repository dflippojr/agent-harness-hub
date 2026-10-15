// HUB-01 navigation and layout: every section loads and survives a reload, the theme follows the color scheme, the
// rail is grouped with its footer, phones get the bottom bar and its More sheet, and no page scrolls sideways at 320,
// 375, 768 or 1440 px.
import { SECTIONS, expect, expectNoHorizontalOverflow, hubUrl, isPhone, openSection, scheme, test } from "./fixtures.mjs";

const BACKGROUND = { light: "#f3f4f6", dark: "#000000" };
const CLI_PARITY_SECTIONS = ["admin", "apps", "marketplace", "modules", "pairing", "settings"];

async function expectTapTargets(scope, context) {
  const targets = scope.locator("button:visible, a:visible, summary:visible, label:has([role=switch]):visible, .command:visible");
  for (const target of await targets.all()) {
    const { name, iconOnly } = await target.evaluate((el) => ({
      name: `${el.tagName.toLowerCase()} ${el.getAttribute("aria-label") || el.textContent.trim() || el.title}`,
      iconOnly: el.matches("button") && !el.textContent.trim(),
    }));
    const box = await target.boundingBox();
    expect(box.height, `${context}: ${name} height`).toBeGreaterThanOrEqual(44);
    if (iconOnly) expect(box.width, `${context}: ${name} width`).toBeGreaterThanOrEqual(44);
  }
}

test("each section loads, is marked current and survives a reload", async ({ signedIn: page, errors }, testInfo) => {
  await expect(page.getByRole("switch", { name: "Show host commands", includeHidden: true })).not.toBeChecked();
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()))
    .toBe(BACKGROUND[scheme(testInfo)]);
  for (const section of SECTIONS) {
    await openSection(page, section.id);
    if (CLI_PARITY_SECTIONS.includes(section.id)) {
      await expect(page.getByRole("button", { name: /^Host commands?$/ }).first(),
        `${section.label} offers CLI parity with the switch off`).toBeVisible();
    }
    await expect(page).toHaveTitle(`${section.label} · Hub`);
    await expect(page.locator(`#nav a[data-section="${section.id}"]`)).toHaveAttribute("aria-current", "page");
    await page.reload();
    await expect(section.ready(page).first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("the rail groups the sections, shows the daemon chip and banner, and remembers the host-commands switch", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(isPhone(testInfo), "phones have the bottom bar instead of the rail; the phone test covers it");
  const nav = page.getByRole("navigation", { name: "Sections" });
  await expect(page.locator("#tabbar")).toBeHidden();
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
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual(["hub.hostCommands"]);
  await expect(page.locator("body")).toHaveClass(/show-host-commands/);
  await expect(keysList).toBeVisible();
  await expect(reveal).toBeHidden();
  await page.reload();
  await expect(host).toBeChecked();
  await expect(keysList).toBeVisible();
  await host.uncheck();
  // Turning the switch off removes its key; no other browser preferences or credentials are stored here.
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([]);
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

test("phone: pages fit the device width and the bottom bar navigates", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(!isPhone(testInfo), "phone projects only");
  for (const section of SECTIONS) {
    await openSection(page, section.id);
    await expectNoHorizontalOverflow(page);
    await expectTapTargets(page.getByRole("main"), section.label);
    await page.getByRole("button", { name: "More", exact: true }).click();
    const drawer = page.getByRole("navigation", { name: "More sections" });
    await expect(drawer).toBeVisible();
    // Measure the open sheet after its slide transition, avoiding fractional transformed boxes in WebKit.
    await drawer.evaluate((el) => Promise.all(el.getAnimations().map((animation) => animation.finished)));
    await expectTapTargets(drawer, `${section.label} drawer`);
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
  }
  const bar = page.getByRole("navigation", { name: "Sections" });
  const more = bar.getByRole("button", { name: "More" });
  const sheet = page.getByRole("navigation", { name: "More sections" });
  await page.goto(`${hubUrl()}/#/overview`);
  await expect(bar.getByRole("link")).toHaveText(["Overview", /^Pairing/, "Apps"]);
  await expect(bar.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
  // No rail and no drawer: the daemon chip sits beside the page title, and the sheet is closed.
  await expect(page.locator("main > .page-head").getByRole("status")).toHaveText("Daemon live · API 1.24");
  await expect(page.locator("#nav")).toBeHidden();
  await bar.getByRole("link", { name: "Apps" }).click();
  await expect(page.getByRole("heading", { name: "Apps", level: 1 })).toBeVisible();
  await expect(bar.getByRole("link", { name: "Apps" })).toHaveAttribute("aria-current", "page");

  // More opens the rest of the sections as a sheet, with the footer's switch and Sign out.
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await more.click();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await expect(sheet.getByRole("link"))
    .toHaveText(["Spend", "Marketplace", "Settings", "Modules", "Accounts and maintenance"]);
  await expect(sheet.getByRole("switch", { name: "Show host commands" })).toBeVisible();
  await expect(sheet.getByRole("button", { name: "Sign out" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await expect(more).toBeFocused();
  await expect(sheet).toBeHidden();
  // A section from the sheet closes it and lights up More.
  await more.click();
  await sheet.getByRole("link", { name: "Modules" }).click();
  await expect(page.getByRole("heading", { name: "Modules", level: 1 })).toBeVisible();
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await expect(more).toHaveClass(/current/);
  await expect(page.locator('#nav a[data-section="modules"]')).toHaveAttribute("aria-current", "page");
  expect(errors).toEqual([]);
});

test("live resize keeps a partly filled form and a working bottom bar", async ({ signedIn: page, errors }, testInfo) => {
  test.skip(isPhone(testInfo), "desktop projects resize; phones have a fixed device viewport");
  await openSection(page, "settings");
  const turns = page.getByRole("spinbutton", { name: "Maximum turns" });
  const original = await turns.inputValue();
  await turns.fill(String(Number(original) + 7));
  const more = page.getByRole("button", { name: "More" });
  for (const width of [1280, 1024, 768, 414, 375]) await page.setViewportSize({ width, height: 900 });
  await expect(more).toBeVisible();
  await expect(page.locator("main > .page-head > #daemon-chip")).toBeVisible();
  await more.click();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("link", { name: "Modules" })).toBeVisible();
  for (const width of [768, 1024, 1440]) await page.setViewportSize({ width, height: 900 });
  await expect(more).toBeHidden();
  await expect(turns).toHaveValue(String(Number(original) + 7));
  await expectNoHorizontalOverflow(page);
  // The rail works, the daemon chip is back in its footer, and nothing left from the sheet covers the page.
  await expect(page.getByRole("navigation", { name: "Sections" }).getByRole("status")).toBeVisible();
  await turns.click();
  await expect(turns).toBeFocused();
  await page.getByRole("link", { name: "Modules" }).click();
  await expect(page.getByRole("heading", { name: "Modules", level: 1 })).toBeVisible();
  expect(errors).toEqual([]);
});
