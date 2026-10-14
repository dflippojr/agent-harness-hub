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
        { json: { configured: { max_running: null, max_queued: 10 }, effective: { max_running: 2, max_queued: 10 },
          running: 1, queued: 0, parked: 1, retention_days: null } });
      if (pathname.endsWith("/retention")) return route.fulfill({ json: { id: pathname.split("/")[3], retention_days: 7 } });
      if (pathname.endsWith("/revoke")) return route.fulfill({ status: 204 });
      throw Error("Unexpected API call: " + pathname);
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<link rel="stylesheet" href="/static/style.css"><main id="main"></main><script type="module">import {renderApps} from "/static/pages/apps.mjs"; renderApps(document.querySelector("main"));</script>' });
    if (!/^\/static\/[a-z/.-]+\.(mjs|css)$/.test(pathname)) throw Error("Unexpected resource: " + pathname);
    return route.fulfill({ contentType: pathname.endsWith(".css") ? "text/css" : "text/javascript", body:
      await readFile(path.join(__dirname, "../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.locator("[data-app-id]")).toHaveCount(rows.length);
  return { writes, errors };
}
const row = (page, id) => page.locator('[data-app-id="' + id + '"]');
// A row's button opens its detail beside the list (or in its place on a narrow screen); a second press closes it.
async function open(page, id) {
  const detail = page.locator('[data-detail-id="' + id + '"]');
  if (!await detail.count()) await row(page, id).getByRole("button").click();
  await expect(detail).toBeVisible();
  return detail;
}

test("redacted App remains revocable, owner and runner keys never enter the App flow", async ({ page }) => {
  const rows = [app("k-shaped", "[withheld: token-shaped]", { origins: ["https://hp-x.example"] }),
    app("k-device", "Device", { kind: "device" }),
    app("k-pending-device", "Pending device", { kind: "device", revoked_at: 1700000001, state: "erasure_pending",
      erase_after: 1900000000 }),
    app("k-owner", "Owner CLI", { kind: "owner", role: "owner", scopes: "admin" }),
    app("k-runner", "Mac runner", { kind: "owner", role: "runner", scopes: "admin" }),
    app("k-revoked-owner", "Revoked owner", { kind: "owner", revoked_at: 1700000001 }),
    app("k-erased", "Erased", { revoked_at: 1700000001, erased_at: 1700000002 }),
    app("k-web", "Web", { kind: "web" }), app("k-hub", "Hub", { kind: "owner", role: "hub", this_hub: true })];
  const { writes, errors } = await mount(page, rows, { erasures: [rows.find(row => row.id === "k-revoked-owner"),
    rows.find(row => row.id === "k-pending-device")] });
  await expect((await open(page, "k-shaped")).getByRole("button", { name: /^Revoke/ })).toBeEnabled();
  const device = await open(page, "k-device");
  await expect(device.getByRole("button", { name: /^Revoke/ })).toBeEnabled();
  await expect(device.getByRole("button", { name: "Save session caps" })).toHaveCount(0);
  // A revoked App's row shows its erasure and leads to the host-only restore command, which always shows.
  await expect(row(page, "k-pending-device")).toContainText("Revoked · erases");
  await expect(row(page, "k-pending-device").getByRole("button", { name: "Restore command for Pending device" }))
    .toBeVisible();
  await expect((await open(page, "k-pending-device")).getByText("harness apps restore k-pending-device", { exact: true }))
    .toBeVisible();
  for (const id of ["k-owner", "k-runner", "k-revoked-owner", "k-erased", "k-web", "k-hub"]) {
    const detail = await open(page, id);
    await expect(detail.getByRole("button", { name: /^(Revoke|Save|Confirm)/ })).toHaveCount(0);
    await expect(detail.getByText(/harness apps restore/)).toHaveCount(0);
  }
  await expect(await open(page, "k-owner")).toContainText("revocation stops CLI or runner access at once");
  await expect(await open(page, "k-hub")).toContainText("harness hub release --confirm");
  await expect(page.getByText("harness apps restore k-revoked-owner", { exact: true })).toHaveCount(0);
  const shaped = await open(page, "k-shaped");
  await shaped.getByRole("button", { name: /^Revoke/ }).click();
  await expect(shaped.locator(".danger-zone [role=alert]").getByRole("heading", { name: /^Revoke/ })).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(shaped.getByRole("button", { name: /^Revoke/ })).toBeFocused();
  expect(writes).toHaveLength(0);
  await shaped.getByRole("button", { name: /^Revoke/ }).click();
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
  for (const id of ["k-real", "k-copy"]) {
    await expect(row(page, id)).toContainText("Duplicate name");
    await expect(await open(page, id)).toContainText("matches another paired key");
  }
  await expect(row(page, "k-impostor")).toContainText("Reserved name");
  await expect(await open(page, "k-impostor")).toContainText("reserved This Hub label");
  await expect(row(page, "k-hub")).not.toContainText("Reserved name");
  await expect(await open(page, "k-hub")).not.toContainText("reserved This Hub label");
  await expect(row(page, "k-copy")).toContainText("k-copy · App-supplied name");
  const copy = await open(page, "k-copy");
  await expect(copy.getByRole("heading", { level: 2 })).toHaveText("web client");
  await expect(copy.locator(".apps-detail-head")).toContainText("k-copy");
  await expect((await open(page, "k-markup")).getByRole("heading", { level: 2 })).toHaveText("<img src=x onerror=alert(1)>");
  await expect(page.locator("img")).toHaveCount(0);
  await (await open(page, "k-copy")).getByRole("button", { name: /^Revoke/ }).click();
  const dialog = page.locator(".apps-detail [role=alert]");
  for (const text of ["k-copy", "Origins", "https://app.test", "Catalog app", "catalog-web", "Created",
    "Verified entry pairing", "No verified entry pairs this key id", "App-supplied name: web client"]) {
    await expect(dialog).toContainText(text);
  }
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await (await open(page, "k-real")).getByRole("button", { name: /^Revoke/ }).click();
  await expect(dialog.locator("dd").last()).toHaveText("catalog-web");
  // Catalog entries pair only through the daemon's verified ids; an unsigned one says so.
  await expect(page.locator('[data-entry-id="catalog-web"]')).toContainText("Paired: Web client");
  await expect(page.locator('[data-entry-id="catalog-copy"]')).toContainText("Unsigned local entry");
  expect(errors).toEqual([]);
});

test("invalid erasure and limits responses show errors and never offer default resets", async ({ page }) => {
  const { writes, errors } = await mount(page, [app("k-one", "One")], { erasures: null, invalidDetails: true });
  await expect(page.locator(".apps-erasures")).toContainText("invalid erasure data");
  const detail = await open(page, "k-one");
  await expect(detail.getByText("The daemon returned invalid session limits.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save session caps", exact: true })).toHaveCount(0);
  await expect(page.locator("input")).toHaveCount(0);
  expect(writes).toHaveLength(0);
  expect(errors).toEqual([]);
});

test("a healthy inventory shows no empty error box; a failed read still shows its message", async ({ page }) => {
  const { errors } = await mount(page, [app("k-ok", "Synthetic notes")]);
  await expect(page.getByRole("heading", { name: "Paired apps and keys" })).toBeVisible();
  const alerts = page.locator("[role=alert]:visible");
  for (const alert of await alerts.all()) await expect(alert).not.toHaveText("");
  await expect(page.locator(".notice.bad:visible")).toHaveCount(0);
  await page.route("http://hub.test/api/apps", route => route.fulfill({ status: 502,
    json: { detail: "The daemon returned invalid app inventory." } }));
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The daemon returned invalid app inventory." })).toBeVisible();
  expect(errors).toEqual([]);
});

test("readable states in the table, the detail beside it, caps with the default as placeholder, in-card retention",
  async ({ page }) => {
    const now = Date.now() / 1000;
    const rows = [app("k-live", "Live app", { last_used_at: now - 30, errors: true, store: {
        sessions: { running: 2, done: 3 }, usage: { requests: 1234, prompt_tokens: 100, completion_tokens: 40, cost_usd: 0.5 },
        errors: 1, last_error: "provider_limit", last_error_at: now - 600 } }),
      app("k-new", "New app", { state: "never_used" }),
      app("k-idle", "Idle app", { state: "idle", last_used_at: now - 7200 }),
      app("k-old", "Old app", { state: "stale", token_age_seconds: 120 * 86400, token_age_warning: true }),
      app("k-gone", "Gone app", { state: "erasure_pending", revoked_at: now - 60, erase_after: now + 5 * 86400 })];
    const { writes, errors } = await mount(page, rows, { erasures: [rows[4]] });
    for (const [id, label] of [["k-live", "Active"], ["k-new", "Never used"], ["k-idle", "Idle"], ["k-old", "Stale"],
      ["k-gone", "Revoked · erases"]]) await expect(row(page, id).locator(".app-status")).toContainText(label);
    await expect(row(page, "k-live")).toContainText("Errors, 24 h");
    await expect(row(page, "k-live")).toContainText("Just now");
    await expect(row(page, "k-old")).toContainText("Token 90+ days");
    await expect(row(page, "k-old")).toContainText("120 days");
    await expect(page.locator("main")).not.toContainText(/never_used|erasure_pending|1700000000/);

    await page.setViewportSize({ width: 1440, height: 900 });
    const detail = await open(page, "k-live");
    await expect(detail.getByRole("heading", { level: 2, name: "Live app" })).toBeFocused();
    await expect(row(page, "k-live")).toHaveClass(/selected/);
    for (const text of ["Requests", "1,234", "Tokens in · out", "100 · 40", "Cost", "$0.50", "Running 2 · done 3",
      "Errors in the last 24 hours: provider_limit"]) await expect(detail).toContainText(text);
    // Once the caps have loaded (a redraw), the heading still has focus and the list sits beside the detail.
    await expect(detail.getByLabel("Maximum running (1–100)")).toBeVisible();
    await expect(detail.getByRole("heading", { level: 2, name: "Live app" })).toBeFocused();
    const [list, pane] = [await page.locator(".apps-keys").boundingBox(), await detail.boundingBox()];
    expect(list.x + list.width).toBeLessThanOrEqual(pane.x);
    const running = detail.getByLabel("Maximum running (1–100)");
    await expect(running).toHaveValue("");
    await expect(running).toHaveAttribute("placeholder", "Default 2");
    await expect(detail.getByLabel("Maximum queued (1–1000)")).toHaveValue("10");
    await expect(detail.getByLabel("Retention days")).toHaveAttribute("placeholder", "Keep until deleted");
    await expect(detail).toContainText("Effective caps: running 2 · queued 10");
    await running.fill("4");
    await detail.getByRole("button", { name: "Save session caps" }).click();
    await expect(page.getByText("Session caps saved.", { exact: true })).toBeVisible();
    // Only the changed cap goes out; the daemon keeps the one left out.
    expect(writes[0]).toEqual({ path: "/api/apps/k-live/limits", method: "PUT", body: { max_running: 4 } });
    await expect(detail.getByLabel("Retention days")).toBeVisible();

    await detail.getByLabel("Retention days").fill("7");
    await detail.getByRole("button", { name: "Save retention" }).click();
    const ask = detail.locator(".apps-form [role=alert]");
    await expect(ask.getByRole("heading", { name: "Change retention for Live app?" })).toBeVisible();
    await expect(ask).toContainText("Apply retention 7 days to Live app.");
    expect(writes).toHaveLength(1);
    await ask.getByRole("button", { name: "Cancel" }).click();
    await expect(detail.getByRole("button", { name: "Save retention" })).toBeFocused();
    await expect(detail.getByLabel("Retention days")).toHaveValue("7");
    await detail.getByRole("button", { name: "Save retention" }).click();
    await ask.getByRole("button", { name: "Confirm change retention for Live app" }).click();
    await expect(page.getByText("Retention saved.", { exact: true })).toBeVisible();
    expect(writes[1]).toEqual({ path: "/api/apps/k-live/retention", method: "PUT", body: { retention_days: 7, confirm: true } });

    // Below 1200px the detail takes the list's place; Back to all apps returns to it.
    await page.setViewportSize({ width: 1024, height: 768 });
    await expect(page.locator(".apps-keys")).toBeHidden();
    await detail.getByRole("button", { name: "Back to all apps" }).click();
    await expect(detail).toHaveCount(0);
    await expect(row(page, "k-live").getByRole("button", { name: "Open Live app" })).toBeFocused();
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 900 });
      if (width === 390) await open(page, "k-live");
      const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
        small: [...document.querySelectorAll("button,input,a")].filter(el => {
          const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.height < 44 || r.width < 44);
        }).map(el => el.textContent) }));
      expect(layout.scroll).toBeLessThanOrEqual(layout.width);
      expect(layout.small).toEqual([]);
    }
    expect(errors).toEqual([]);
  });

test("Refresh reads the open App's caps again, and an older caps reply never replaces a newer one", async ({ page }) => {
  const { errors } = await mount(page, [app("k-a", "App A"), app("k-b", "App B")]);
  const limits = (running) => ({ configured: { max_running: running, max_queued: 10 },
    effective: { max_running: running, max_queued: 10 }, running: 0, queued: 0, parked: 0, retention_days: null });
  let reads = 0, release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("http://hub.test/api/apps/k-a/limits", async route => {
    reads += 1;
    if (reads === 1) { await held; return route.fulfill({ json: limits(2) }); } // the first read answers last
    return route.fulfill({ json: limits(reads === 2 ? 6 : 8) });
  });
  await row(page, "k-a").getByRole("button").click();
  await expect.poll(() => reads).toBe(1);
  const b = await open(page, "k-b");
  await expect(b.getByLabel("Maximum running (1–100)")).toBeVisible();
  await row(page, "k-a").getByRole("button").click();
  const a = page.locator('[data-detail-id="k-a"]');
  await expect(a.getByLabel("Maximum running (1–100)")).toHaveValue("6");
  release();
  await page.waitForTimeout(300);
  await expect(a.getByLabel("Maximum running (1–100)")).toHaveValue("6");
  // A value changed on the host shows after Refresh, so the next save cannot send the old one back.
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await expect(a.getByLabel("Maximum running (1–100)")).toHaveValue("8");
  expect(reads).toBe(3);
  expect(errors).toEqual([]);
});

test("Refresh drops a caps read still in flight, and a save made during a refresh still happens", async ({ page }) => {
  const rows = [app("k-a", "App A")];
  const { writes, errors } = await mount(page, rows);
  const limits = (running) => ({ configured: { max_running: running, max_queued: 10 },
    effective: { max_running: running, max_queued: 10 }, running: 0, queued: 0, parked: 0, retention_days: null });
  let reads = 0, releaseRead;
  const heldRead = new Promise(resolve => { releaseRead = resolve; });
  await page.route("http://hub.test/api/apps/k-a/limits", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    reads += 1;
    if (reads === 1) { await heldRead; return route.fulfill({ json: limits(2) }); }
    return route.fulfill({ json: limits(6) });
  });
  await row(page, "k-a").getByRole("button").click();
  await expect.poll(() => reads).toBe(1);
  await page.getByRole("button", { name: "Refresh apps" }).click();
  const a = page.locator('[data-detail-id="k-a"]');
  await expect(a.getByLabel("Maximum running (1–100)")).toHaveValue("6");
  releaseRead();
  await page.waitForTimeout(300);
  await expect(a.getByLabel("Maximum running (1–100)")).toHaveValue("6");

  // Hold the next inventory read, as a 30-second poll would be, and save meanwhile: the save waits, then goes out.
  let releaseInventory;
  const heldInventory = new Promise(resolve => { releaseInventory = resolve; });
  await page.route("http://hub.test/api/apps", async route => {
    await heldInventory;
    return route.fulfill({ json: { state: "ready", apps: rows, entries: [] } });
  });
  await a.getByLabel("Maximum queued (1–1000)").fill("12");
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await a.getByRole("button", { name: "Save session caps" }).click();
  expect(writes).toHaveLength(0);
  releaseInventory();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ path: "/api/apps/k-a/limits", method: "PUT", body: { max_queued: 12 } });
  await expect(page.getByText("Session caps saved.", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("an abandoned caps read never leaves the detail loading, and a read from before a save is dropped", async ({ page }) => {
  const rows = [app("k-a", "App A"), app("k-owner", "Owner CLI", { kind: "owner", role: "owner", scopes: "admin" })];
  const { writes, errors } = await mount(page, rows);
  const limits = (running) => ({ configured: { max_running: running, max_queued: 10 },
    effective: { max_running: running, max_queued: 10 }, running: 0, queued: 0, parked: 0, retention_days: null });
  const gates = [];
  const gate = () => { let open; const shut = new Promise(resolve => { open = resolve; }); gates.push(open); return shut; };
  let reads = 0;
  const plan = { 1: [true, 2], 2: [false, 3], 3: [true, 2] }; // [held, running]; later reads answer 7
  await page.route("http://hub.test/api/apps/k-a/limits", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    const [held, running] = plan[++reads] || [false, 7];
    if (held) await gate();
    return route.fulfill({ json: limits(running) });
  });
  const a = page.locator('[data-detail-id="k-a"]');
  const field = a.getByLabel("Maximum running (1–100)");
  // Leave A while its read is held, let that read land, and come back: A reads again.
  await row(page, "k-a").getByRole("button").click();
  await expect.poll(() => reads).toBe(1);
  await open(page, "k-owner");
  gates.shift()();
  await page.waitForTimeout(200);
  await row(page, "k-a").getByRole("button").click();
  await expect(field).toHaveValue("3");

  // Save while Refresh reads the inventory: the caps read that Refresh starts is older than the save, so it is dropped.
  await expect.poll(() => reads).toBe(2);
  await field.fill("");
  await field.fill("3");
  await a.getByLabel("Maximum queued (1–1000)").fill("12");
  let releaseInventory;
  const heldInventory = new Promise(resolve => { releaseInventory = resolve; });
  await page.route("http://hub.test/api/apps", async route => {
    await heldInventory;
    return route.fulfill({ json: { state: "ready", apps: rows, entries: [] } });
  });
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await a.getByRole("button", { name: "Save session caps" }).click();
  releaseInventory();
  await expect.poll(() => writes.length).toBe(1);
  await expect.poll(() => reads).toBeGreaterThanOrEqual(4);
  await expect(field).toHaveValue("7");
  while (gates.length) gates.shift()();
  await page.waitForTimeout(300);
  await expect(field).toHaveValue("7");
  expect(errors).toEqual([]);
});

test("an unfinished number stays invalid across a redraw, and an unchanged form sends nothing", async ({ page }) => {
  const { writes, errors } = await mount(page, [app("k-a", "App A")]);
  const a = await open(page, "k-a");
  const running = a.getByLabel("Maximum running (1–100)");
  await expect(running).toBeVisible();
  await a.getByRole("button", { name: "Save session caps" }).click();
  await expect(page.getByText("Nothing to save: both caps are as the daemon has them.", { exact: true })).toBeVisible();
  await running.pressSequentially("1e");
  // Asking the retention question redraws the detail; the half-typed cap keeps its own element and stays invalid.
  await a.getByLabel("Retention days").fill("5");
  await a.getByRole("button", { name: "Save retention" }).click();
  await a.getByRole("button", { name: "Cancel" }).click();
  await expect.poll(() => running.evaluate((input) => input.validity.badInput)).toBe(true);
  await a.getByRole("button", { name: "Save session caps" }).click();
  await page.waitForTimeout(200);
  expect(writes).toHaveLength(0);
  expect(errors).toEqual([]);
});

test("the retention question locks the field it applies, and a row click waits while a save runs", async ({ page }) => {
  const { writes, errors } = await mount(page, [app("k-a", "App A"), app("k-b", "App B")]);
  const a = await open(page, "k-a");
  const days = a.getByLabel("Retention days");
  await days.fill("7");
  await a.getByRole("button", { name: "Save retention" }).click();
  await expect(days).toBeDisabled();
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await expect(a.getByRole("heading", { name: "Change retention for App A?" })).toBeVisible();
  await expect(days).toBeDisabled();
  await a.getByRole("button", { name: "Confirm change retention for App A" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body).toEqual({ retention_days: 7, confirm: true });
  await expect(days).toBeEnabled();

  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("http://hub.test/api/apps/k-a/limits", async route => {
    if (route.request().method() === "PUT") await held;
    return route.fallback();
  });
  await a.getByLabel("Maximum queued (1–1000)").fill("12");
  await a.getByRole("button", { name: "Save session caps" }).click();
  await row(page, "k-b").click();
  await expect(page.locator('[data-detail-id="k-b"]')).toHaveCount(0);
  release();
  await expect(page.getByText("Session caps saved.", { exact: true })).toBeVisible();
  await expect(a).toBeVisible();
  expect(errors).toEqual([]);
});

test("Refresh drops typed caps, so a save after it cannot send a value the host has since changed", async ({ page }) => {
  const { writes, errors } = await mount(page, [app("k-a", "App A")]);
  let running = 4;
  await page.route("http://hub.test/api/apps/k-a/limits", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: { configured: { max_running: running, max_queued: 10 },
      effective: { max_running: running, max_queued: 10 }, running: 0, queued: 0, parked: 0, retention_days: null } });
  });
  const a = await open(page, "k-a");
  const field = a.getByLabel("Maximum running (1–100)");
  await field.fill("5");
  await field.fill("4");
  running = 8; // changed on the host
  await page.getByRole("button", { name: "Refresh apps" }).click();
  await expect(field).toHaveValue("8");
  await a.getByLabel("Maximum queued (1–1000)").fill("12");
  await a.getByRole("button", { name: "Save session caps" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body).toEqual({ max_queued: 12 });
  expect(errors).toEqual([]);
});
