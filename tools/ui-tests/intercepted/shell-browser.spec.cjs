// Run: npm --prefix tools/ui-tests test -- --project=intercepted
// The real index.html and app shell over synthetic intercepted Hub responses: no daemon, Hub server or real account.
const { test, expect } = require("@playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");

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

async function mount(page, hash, { signedIn = true, memoryReply = memory, hubDown = false, extraReplies = {} } = {}) {
  const answers = { ...replies, ...extraReplies };
  const posts = [], errors = [], missing = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  await page.route("http://hub.test/**", async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/")) {
      // true: every Hub read fails; a RegExp: only the reads whose path matches it.
      if (hubDown === true || (hubDown instanceof RegExp && hubDown.test(pathname))) return route.abort("connectionrefused");
      const resource = pathname.slice(5);
      if (resource === "session" && request.method() === "POST") {
        posts.push(request.postDataJSON());
        return route.fulfill({ status: 401, json: { detail: "that sign-in link is not valid; links work once, for ten minutes" } });
      }
      if (resource === "session") return route.fulfill({ json: { signed_in: signedIn,
        signin_command: "python -m harness_hub signin-link" } });
      if (resource === "settings/memory") return route.fulfill({ json: memoryReply });
      if (resource in answers) return route.fulfill({ json: answers[resource] });
      missing.push(resource);
      return route.fulfill({ status: 503, json: { detail: "Synthetic: not part of this test." } });
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html",
      body: await readFile(path.join(__dirname, "../../../harness_hub/static/index.html"), "utf8") });
    if (!/^\/static\/[a-z/.-]+\.(mjs|css|svg)$/.test(pathname)) throw Error("Unexpected resource: " + pathname);
    const type = { mjs: "text/javascript", css: "text/css", svg: "image/svg+xml" }[pathname.split(".").pop()];
    return route.fulfill({ contentType: type, body: await readFile(path.join(__dirname, "../../../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/" + hash);
  return { posts, errors, missing };
}

// Intercepted: a memory library module; the instance turns memory_library off.
test("Settings still offers the profile editor when the module is present", async ({ page }) => {
  await mount(page, "#/settings");
  await page.getByRole("button", { name: "Memory agent profile" }).click();
  await expect(page.getByLabel("Agent profile", { exact: true })).toHaveValue("Synthetic profile");
  await expect(page.getByRole("button", { name: "Save profile" })).toBeVisible();
});

// Intercepted: the daemon going silent behind a running Hub (forced 503s).
test("a daemon that stops answering raises the banner under the page head, and Retry now reads the page again",
  async ({ page }) => {
    const { errors } = await mount(page, "#/modules");
    await expect(page.locator("tr[data-module=hub]")).toContainText("Running");
    // Every read now finds the daemon silent: the Hub's own 503, marked reachable false.
    let reads = 0;
    await page.route(/\/api\/(modules|settings\/)/, route => {
      reads += 1;
      return route.fulfill({ status: 503, json: { detail: "The daemon did not answer.", reachable: false } });
    });
    await page.getByRole("button", { name: "Refresh modules" }).click();
    const slot = page.locator("#banner-slot");
    await expect(slot.getByRole("alert")).toContainText("The daemon is not answering.");
    // The slot sits right under the page head.
    expect(await page.evaluate(() => document.querySelector("#main > .page-head").nextElementSibling.id)).toBe("banner-slot");
    const before = reads;
    await slot.getByRole("button", { name: "Retry now" }).click();
    await expect.poll(() => reads).toBeGreaterThan(before);
    expect(errors.filter(error => !error.includes("503"))).toEqual([]);
  });

// Intercepted: the daemon going silent and answering again while edits are unsaved.
test("Retry now on Settings reads again in place and keeps unsaved edits", async ({ page }) => {
  const turnsSpec = { key: "sessions.max_turns", label: "Maximum turns", type: "int", minimum: 1, maximum: 500,
    category: "Sessions", apply: "live", default: 80, writable: true };
  const { errors } = await mount(page, "#/settings", { extraReplies: {
    "settings/schema": { settings: [turnsSpec] },
    "settings/config": { ...replies["settings/config"], settings: [{ key: turnsSpec.key, effective: 80, source: "file" }] } } });
  const turns = page.getByRole("spinbutton", { name: "Maximum turns" });
  await expect(turns).toHaveValue("80");
  await turns.fill("87");
  // An edit in a control's editor too.
  await page.getByRole("button", { name: "Memory agent profile" }).click();
  const profile = page.getByLabel("Agent profile", { exact: true });
  await expect(profile).toHaveValue("Synthetic profile");
  await profile.fill("Edited profile");
  // The daemon stops answering; the 5 s health poll raises the banner.
  const silent = route => route.fulfill({ status: 503, json: { detail: "The daemon did not answer.", reachable: false } });
  await page.route(/\/api\/settings\//, silent);
  const slot = page.locator("#banner-slot");
  await expect(slot.getByRole("alert")).toContainText("The daemon is not answering.", { timeout: 10000 });
  // It answers again before Retry now: the reads repeat, and the typed value stays.
  await page.unroute(/\/api\/settings\//, silent);
  await slot.getByRole("button", { name: "Retry now" }).click();
  await expect(slot.getByRole("alert")).toHaveCount(0);
  await expect(profile).toHaveValue("Edited profile");
  await page.getByRole("button", { name: "Sessions" }).click();
  await expect(turns).toHaveValue("87");
  expect(errors.filter(error => !error.includes("503"))).toEqual([]);
});

// Intercepted: the Hub server not answering, then answering while the daemon does not.
test("Settings drops the Hub-down state when Retry reaches the Hub but the daemon still does not answer",
  async ({ page }) => {
    const { errors } = await mount(page, "#/settings", { hubDown: /^\/api\/settings\// });
    const hubDown = page.getByRole("heading", { name: "The Hub server is not answering" });
    await expect(hubDown).toBeVisible();
    // The Hub answers again; the daemon behind it still does not.
    await page.route(/\/api\/settings\//, route =>
      route.fulfill({ status: 503, json: { detail: "The daemon did not answer.", reachable: false } }));
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.locator("main")).toContainText("The daemon did not answer.");
    await expect(hubDown).toHaveCount(0);
    expect(errors.filter(error => !/ERR_CONNECTION_REFUSED|503/.test(error))).toEqual([]);
  });

// Intercepted: the Hub server itself not answering (aborted requests).
test("a Hub server that does not answer at start shows its state and a Retry, never \"Failed to fetch\"",
  async ({ page }) => {
    const { errors } = await mount(page, "#/overview", { hubDown: true });
    await expect(page.getByRole("heading", { name: "The Hub server is not answering" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
    await expect(page.locator("main")).not.toContainText("Failed to fetch");
    expect(errors.filter(error => !error.includes("ERR_CONNECTION_REFUSED"))).toEqual([]);
  });

// Intercepted: a claim whose key reply never arrived, which the daemon cannot be made to lose.
test("a claim whose key reply was lost shows the release command beside Start a new claim", async ({ page }) => {
  const claim = { state: "lost", release_command: "harness hub release --confirm",
    message: "The daemon issued this Hub's key, but the reply never arrived. On the daemon host run "
      + "harness hub release --confirm, then start a new claim here." };
  const { errors } = await mount(page, "#/overview", { extraReplies: { overview: {
    reachable: true, restarting: false, error: "", fetched_at: Date.now() / 1000,
    hub: { version: "0.0.0", daemon_url: "http://127.0.0.1:8085" },
    daemon: {}, health: { ok: true }, capabilities: {}, ownership: null, claim } } });
  const card = page.getByRole("region", { name: "Claim this daemon" });
  await expect(card).toContainText("Key lost");
  await expect(card.getByRole("alert")).toHaveText(claim.message);
  await expect(card).toContainText("First, on the daemon host run");
  await expect(card.getByText("harness hub release --confirm", { exact: true })).toBeVisible();
  await expect(card.getByRole("button", { name: "Copy: harness hub release --confirm" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Start a new claim" })).toBeEnabled();
  expect(errors.filter(error => !error.includes("503"))).toEqual([]);
});
