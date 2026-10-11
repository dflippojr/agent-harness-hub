// Run: playwright test --config=tests/playwright.config.cjs --workers=1
// All network requests are intercepted; no daemon or Hub server is started.
const { test, expect } = require("playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");

const app = (id, name, extra = {}) => ({
  id, name, kind: "app", role: "app", state: "active", this_hub: false,
  scopes: "sessions", origins: ["https://app.test"], catalog_app_id: "catalog-web",
  created_at: 1700000000, last_used_at: null, token_age_seconds: 100,
  revoked_at: null, erase_after: null, erased_at: null,
  store: { sessions: {}, usage: {}, errors: 0, last_error: "", last_error_at: null },
  ...extra,
});

async function mount(page, rows, { entries = [], erasures = [], invalidDetails = false } = {}) {
  const writes = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://hub.test/**", async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/")) {
      if (request.method() !== "GET") writes.push({ path: pathname, method: request.method(), body: request.postDataJSON() });
      if (pathname === "/api/apps") return route.fulfill({ json: { state: "ready", apps: rows, entries } });
      if (pathname === "/api/apps/erasures") return route.fulfill(Array.isArray(erasures) ? { json: erasures } :
        { status: 502, json: { detail: "The daemon returned invalid erasure data." } });
      if (pathname.endsWith("/limits")) return route.fulfill(invalidDetails ?
        { status: 502, json: { detail: "The daemon returned invalid session limits." } } :
        { json: { configured: { max_running: null, max_queued: 10 }, effective: { max_running: 2, max_queued: 10 } } });
      if (pathname.endsWith("/revoke")) return route.fulfill({ status: 204 });
      throw Error("Unexpected API call: " + pathname);
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<main id="main"></main><script type="module">import {renderApps} from "/static/pages/apps.mjs"; renderApps(document.querySelector("main"));</script>' });
    if (!/^\/static\/[a-z/.-]+\.mjs$/.test(pathname)) throw Error("Unexpected resource: " + pathname);
    return route.fulfill({ contentType: "text/javascript", body:
      await readFile(path.join(__dirname, "../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.locator("[data-app-id]")).toHaveCount(rows.length);
  return { writes, errors };
}
const card = (page, id) => page.locator('[data-app-id="' + id + '"]');

test("redacted App remains revocable, owner and runner keys never enter the App flow", async ({ page }) => {
  const rows = [app("k-shaped", "[withheld: token-shaped]", { origins: ["https://hp-x.example"] }),
    app("k-device", "Device", { kind: "device" }),
    app("k-pending-device", "Pending device", { kind: "device", revoked_at: 1700000001 }),
    app("k-owner", "Owner CLI", { kind: "owner", role: "owner", scopes: "admin" }),
    app("k-runner", "Mac runner", { kind: "owner", role: "runner", scopes: "admin" }),
    app("k-revoked-owner", "Revoked owner", { kind: "owner", revoked_at: 1700000001 }),
    app("k-erased", "Erased", { revoked_at: 1700000001, erased_at: 1700000002 }),
    app("k-web", "Web", { kind: "web" }), app("k-hub", "Hub", { kind: "owner", role: "hub", this_hub: true })];
  const { writes, errors } = await mount(page, rows, { erasures: [rows.find(row => row.id === "k-revoked-owner"), rows.find(row => row.id === "k-pending-device")] });
  await expect(card(page, "k-shaped").getByRole("button", { name: /^Revoke/ })).toBeEnabled();
  await expect(card(page, "k-device").getByRole("button", { name: /^Revoke/ })).toBeEnabled();
  await expect(card(page, "k-device").getByRole("button", { name: /^Manage/ })).toHaveCount(0);
  await expect(card(page, "k-pending-device").getByText("harness apps restore k-pending-device", { exact: true })).toBeVisible();
  for (const id of ["k-owner", "k-runner", "k-revoked-owner", "k-erased", "k-web", "k-hub"]) {
    await expect(card(page, id).getByRole("button", { name: /^(Revoke|Manage)/ })).toHaveCount(0);
    await expect(card(page, id).getByText(/harness apps restore/)).toHaveCount(0);
  }
  await expect(card(page, "k-owner")).toContainText("revocation stops CLI or runner access at once");
  await expect(page.getByText("harness apps restore k-revoked-owner", { exact: true })).toHaveCount(0);
  await card(page, "k-shaped").getByRole("button", { name: /^Revoke/ }).click();
  await expect(page.getByRole("heading", { name: /^revoke/ })).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(writes).toHaveLength(0);
  await card(page, "k-shaped").getByRole("button", { name: /^Revoke/ }).click();
  await page.getByRole("button", { name: /^Confirm revoke/ }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ path: "/api/apps/k-shaped/revoke", method: "DELETE", body: { confirm: true } });
  expect(errors).toEqual([]);
});

test("duplicate and reserved names are flagged and confirmation identifies the actual key", async ({ page }) => {
  const rows = [app("k-real", "Web client"), app("k-copy", "web client"), app("k-impostor", "This Hub"),
    app("k-hub", "Actual Hub", { kind: "owner", role: "hub", this_hub: true }),
    app("k-markup", "<img src=x onerror=alert(1)>")];
  const { writes, errors } = await mount(page, rows, { entries: [
    { name: "Web entry", catalog_app_id: "catalog-web", paired: ["k-real"], verified: true },
    { name: "Untrusted entry", catalog_app_id: "catalog-copy", paired: ["k-copy"], verified: false }] });
  for (const id of ["k-real", "k-copy"]) await expect(card(page, id)).toContainText("matches another paired key");
  await expect(card(page, "k-impostor")).toContainText("reserved This Hub label");
  await expect(card(page, "k-hub").getByText(/reserved This Hub label/)).toHaveCount(0);
  await expect(card(page, "k-copy")).toContainText("App-supplied name · k-copy");
  await expect(card(page, "k-copy").getByRole("heading")).toHaveText("web client · k-copy");
  await expect(card(page, "k-markup").getByRole("heading")).toHaveText("<img src=x onerror=alert(1)> · k-markup");
  await expect(page.locator("img")).toHaveCount(0);
  await card(page, "k-copy").getByRole("button", { name: /^Revoke/ }).click();
  const dialog = page.locator("section[role=alert]");
  for (const text of ["k-copy", "Origins", "https://app.test", "Catalog app", "catalog-web", "Created",
    "Verified entry pairing", "No verified entry pairs this key id", "App-supplied name: web client"]) {
    await expect(dialog).toContainText(text);
  }
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await card(page, "k-real").getByRole("button", { name: /^Revoke/ }).click();
  await expect(dialog.locator("dd").last()).toHaveText("catalog-web");
  expect(errors).toEqual([]);
});

test("invalid erasure and limits responses show errors and never offer default resets", async ({ page }) => {
  const { writes, errors } = await mount(page, [app("k-one", "One")], { erasures: null, invalidDetails: true });
  await expect(page.locator(".apps-erasures")).toContainText("invalid erasure data");
  await expect(page.getByText("No apps are waiting to be erased.", { exact: true })).toHaveCount(0);
  await card(page, "k-one").getByRole("button", { name: /^Manage/ }).click();
  await expect(page.getByText("The daemon returned invalid session limits.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save session caps", exact: true })).toHaveCount(0);
  await expect(page.locator("input")).toHaveCount(0);
  expect(writes).toHaveLength(0);
  expect(errors).toEqual([]);
});
