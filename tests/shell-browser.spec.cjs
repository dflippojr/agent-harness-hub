// Run: playwright test --config=tests/playwright.config.cjs --workers=1 shell-browser
// The real index.html and app shell over synthetic intercepted Hub responses: no daemon, Hub server or real account.
const { test, expect } = require("playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");

const CODE = "A".repeat(32);
const replies = {
  "modules": { state: "ready", modules: [{ name: "hub", state: "present" }], error: "", stale: false },
  "settings/schema": { settings: [] },
  "settings/config": { revision: 1, pending_revision: null, restart_required: false, supervised_restart: true,
    settings: [], warning: "" },
  "settings/smart-approvals": { mode: "off", secret_ref: "" },
  "settings/backends": [],
  "settings/github": { enabled: false, preflight: null },
  "settings/google": { enabled: false, ready: false, preflight: null },
  "settings/health": { reachable: true, restarting: false, health: { config_revision: 1 } },
};
const memory = { installed: true, enabled: true, profile: "Synthetic profile", profile_max_chars: 4000,
  last_commit: {}, refresh_error: "" };

async function mount(page, hash, { signedIn = true, memoryReply = memory } = {}) {
  const posts = [], errors = [], missing = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.route("http://hub.test/**", async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/")) {
      const resource = pathname.slice(5);
      if (resource === "session" && request.method() === "POST") {
        posts.push(request.postDataJSON());
        return route.fulfill({ status: 401, json: { detail: "that sign-in link is not valid; links work once, for ten minutes" } });
      }
      if (resource === "session") return route.fulfill({ json: { signed_in: signedIn,
        signin_command: "python -m harness_hub signin-link" } });
      if (resource === "settings/memory") return route.fulfill({ json: memoryReply });
      if (resource in replies) return route.fulfill({ json: replies[resource] });
      missing.push(resource);
      return route.fulfill({ status: 503, json: { detail: "Synthetic: not part of this test." } });
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html",
      body: await readFile(path.join(__dirname, "../harness_hub/static/index.html"), "utf8") });
    if (!/^\/static\/[a-z/.-]+\.(mjs|css|svg)$/.test(pathname)) throw Error("Unexpected resource: " + pathname);
    const type = { mjs: "text/javascript", css: "text/css", svg: "image/svg+xml" }[pathname.split(".").pop()];
    return route.fulfill({ contentType: type, body: await readFile(path.join(__dirname, "../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/" + hash);
  return { posts, errors, missing };
}

for (const hash of ["#signin=bogus-code", "#signin=" + CODE + ".", "#signin=" + CODE + ")", "#signin="]) {
  test(`a malformed sign-in link (${hash.replace(CODE, "<code>")}) is refused without posting it`, async ({ page }) => {
    const { posts, errors } = await mount(page, hash, { signedIn: false });
    await expect(page.getByRole("alert")).toHaveText(
      "That sign-in link is not valid.Links work once, for ten minutes. Make a new one below.");
    expect(new URL(page.url()).hash).toBe("#/overview");
    expect(posts).toEqual([]);
    expect(errors).toEqual([]);
  });
}

test("a plain visit to the sign-in page shows no error", async ({ page }) => {
  const { posts } = await mount(page, "", { signedIn: false });
  await expect(page.getByRole("heading", { name: "Sign in to the Hub" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(posts).toEqual([]);
});

test("a well-formed link is still posted once", async ({ page }) => {
  const { posts } = await mount(page, "#signin=" + CODE, { signedIn: false });
  await expect(page.getByRole("alert")).toContainText("not valid");
  expect(posts).toEqual([{ code: CODE }]);
});

test("Modules with a healthy inventory shows no empty error box", async ({ page }) => {
  const { errors } = await mount(page, "#/modules");
  await expect(page.locator("tr[data-module=hub]")).toContainText("Running");
  await expect(page.locator(".notice.bad:visible")).toHaveCount(0);
  for (const alert of await page.locator("[role=alert]:visible").all()) await expect(alert).not.toHaveText("");
  expect(errors).toEqual([]);
});

test("Settings explains an absent memory library module instead of an error", async ({ page }) => {
  const { errors } = await mount(page, "#/settings", { memoryReply: { ...memory, installed: false, enabled: false, profile: "" } });
  await page.getByRole("button", { name: "Memory agent profile" }).click();
  const box = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "Memory agent profile" }) });
  await expect(box).toContainText("The memory library module (memory_library) is not installed or is switched off");
  await expect(box.getByText("harness config set memory_library.enabled=true", { exact: true })).toBeVisible();
  await expect(box.getByLabel("Agent profile", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Not Found")).toHaveCount(0);
  await expect(box.locator(".notice.bad")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("Settings still offers the profile editor when the module is present", async ({ page }) => {
  await mount(page, "#/settings");
  await page.getByRole("button", { name: "Memory agent profile" }).click();
  await expect(page.getByLabel("Agent profile", { exact: true })).toHaveValue("Synthetic profile");
  await expect(page.getByRole("button", { name: "Save profile" })).toBeVisible();
});

test.describe("phone drawer", () => {
  test.use({ viewport: { width: 375, height: 667 }, hasTouch: true, isMobile: true });

  test("a tap on the backdrop closes the drawer and returns focus to the menu button", async ({ page }) => {
    const { errors } = await mount(page, "#/modules");
    const menu = page.getByRole("button", { name: "Sections" });
    await menu.tap();
    await expect(menu).toHaveAttribute("aria-expanded", "true");
    await page.touchscreen.tap(350, 400);
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(menu).toBeFocused();
    await menu.tap();
    await page.getByRole("navigation").getByRole("link", { name: "Apps" }).tap();
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(new URL(page.url()).hash).toBe("#/apps");
    expect(errors.filter(e => !/503/.test(e))).toEqual([]);
  });

  test("taps inside the drawer leave it open", async ({ page }) => {
    await mount(page, "#/modules");
    const menu = page.getByRole("button", { name: "Sections" });
    await menu.tap();
    // A group label: inside the drawer, but not a link or a footer control.
    await page.locator("#nav .nav-group").first().tap();
    await expect(menu).toHaveAttribute("aria-expanded", "true");
  });
});
