// Run: npm --prefix tools/ui-tests test -- --project=intercepted
// Synthetic intercepted Hub responses: no daemon, Hub server or real account is used.
const { test, expect } = require("@playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");

const NOW = 1700000000;
const ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
const SESSIONS = { scope: "sessions", tier: "standard", text: "This app can start agent tasks on your harness.",
  justification: "Synthetic justification for sessions, long enough for the schema." };
const HOMELAB = { scope: "homelab", tier: "elevated", text: "This app's tasks can **read your homelab services' logs**.",
  justification: "Synthetic justification for homelab, long enough for the schema." };
const entry = (id, extra = {}) => ({
  consent_digest: "a".repeat(64), catalog_app_id: id, name: "Synthetic " + id.split(".").pop(), version: "1.0.0", summary: "Synthetic local entry.",
  publisher: { name: "Synthetic publisher", id: "pub-0a1b2c3d4e5f" }, icon: "", scopes: [SESSIONS], elevated: [],
  browser_origins: [], verified: false, state: "not_paired", paired: [], pending_pairing: false,
  unverified_origin: { paired: [], pending_pairing: false }, slots: [], ...extra,
});
const slot = (id, extra = {}) => ({
  id, kind: "app", state: "armed", needs: "claim", name: "Synthetic", catalog_app_id: "test.example.native", origin: "",
  browser: false, armed: true, scopes: ["sessions"], elevated: [],
  disclosures: [{ scope: "sessions", tier: "standard", text: "Daemon copy for sessions." }],
  key_id: "", created_at: NOW - 60, expires_at: NOW + 540, approved_at: NOW - 60, finished_at: null, ...extra,
});
const view = (entries, extra = {}) => ({ state: "ready", error: "", stale: false, fetched_at: NOW, entries, withheld: 0,
  slots_error: "", daemon_url: "https://daemon.test", now: NOW, ...extra });

async function mount(page, initial, { answer } = {}) {
  const writes = [], errors = [], outside = [];
  let current = initial;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
    if (url.host !== "hub.test") {
      outside.push(req.url());
      return route.abort();
    }
    if (pathname.startsWith("/api/")) {
      if (pathname === "/api/marketplace" && req.method() === "GET") return route.fulfill({ json: current });
      const known = pathname === "/api/marketplace/arm" || /^\/api\/pairing\/pr-[a-z0-9]+\/(confirm|deny)$/.test(pathname);
      if (!known || req.method() !== "POST") throw Error("Unexpected API call: " + req.method() + " " + pathname);
      writes.push({ path: pathname, body: req.postDataJSON(), headers: req.headers() });
      const reply = answer ? answer(pathname, req.postDataJSON()) : null;
      if (reply?.view) current = reply.view;
      return route.fulfill({ status: reply?.status || 200, json: reply?.json || {} });
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<link rel="stylesheet" href="/static/style.css"><main id="main" class="main"></main>' +
      '<script type="module">import {renderMarketplace} from "/static/pages/marketplace.mjs"; renderMarketplace(document.querySelector("main"));</script>' });
    if (!/^\/static\/[a-z/.-]+\.(mjs|css)$/.test(pathname)) throw Error("Unexpected resource: " + pathname);
    return route.fulfill({ contentType: pathname.endsWith(".css") ? "text/css" : "text/javascript",
      body: await readFile(path.join(__dirname, "../../../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.locator("[data-entry]")).toHaveCount((initial.entries || []).length);
  // These regressions read each action's CLI equivalent, so the Show host commands switch is on (Hub design 3, #70).
  await page.evaluate(() => document.body.classList.add("show-host-commands"));
  return { writes, errors, outside };
}
const card = (page, id) => page.locator('[data-entry="' + id + '"]');

test("entries show their state, the not-verified label and native keys; nothing is fetched from an entry", async ({ page }) => {
  const { errors, outside } = await mount(page, view([
    entry("test.example.notes", { state: "paired", paired: ["k-notes"], browser_origins: ["https://notes.example.test"], icon: ICON }),
    entry("test.example.native", { icon: "https://icons.example.invalid/native.png",
      unverified_origin: { paired: ["k-native"], pending_pairing: true } }),
    entry("test.example.ops", { pending_pairing: true }),
  ]));
  await expect(page.getByText("Being listed here grants nothing.", { exact: true })).toBeVisible();
  await expect(card(page, "test.example.notes").locator('[data-field="state"]')).toHaveText("Paired");
  await expect(card(page, "test.example.notes")).toContainText("k-notes");
  await expect(card(page, "test.example.notes")).toContainText("https://notes.example.test");
  await expect(card(page, "test.example.native").locator('[data-field="state"]')).toHaveText("Not paired");
  await expect(card(page, "test.example.ops").locator('[data-field="state"]')).toHaveText("Not paired");
  await expect(card(page, "test.example.ops").locator('[data-field="pending"]')).toHaveText("Pending");
  await expect(card(page, "test.example.ops")).toContainText("The requester chose that id; it is not verified.");
  for (const id of ["test.example.notes", "test.example.native", "test.example.ops"]) {
    await expect(card(page, id).getByText("Local · not verified", { exact: true })).toBeVisible();
  }
  const native = card(page, "test.example.native").locator('[data-field="unverified"]');
  await expect(native).toContainText("Native keys and requests (origin not verified)");
  await expect(native).toContainText("k-native");
  await expect(page.locator("main img")).toHaveCount(1);
  expect(await page.locator("main img").getAttribute("src")).toBe(ICON);
  await expect(card(page, "test.example.native").locator(".entry-icon")).toHaveText("S");
  expect(outside).toEqual([]);
  expect(errors).toEqual([]);
});

test("Set up opens the consent screen and nothing is armed until Approve; then the id and daemon URL are shown", async ({ page }) => {
  const armed = slot("pr-new");
  const { writes, errors, outside } = await mount(page, view([entry("test.example.native")]), { answer: pathname =>
    pathname === "/api/marketplace/arm" ? { status: 201, json: { request: armed, daemon_url: "https://daemon.test",
      name: "Synthetic native" }, view: view([entry("test.example.native", { slots: [armed] })]) } : null });
  await card(page, "test.example.native").getByRole("button", { name: "Set up Synthetic native" }).click();
  const consent = page.locator(".market-consent");
  await expect(consent).toContainText("Arming grants scope consent.");
  await expect(consent).toContainText("Both browser and native claims wait for you to type the code");
  await expect(consent.getByRole("heading", { name: "Set up Synthetic native (test.example.native)?" })).toBeVisible();
  await expect(consent.locator('[data-scope="sessions"] .disclosure-text')).toHaveText(SESSIONS.text);
  await expect(consent).toContainText("Publisher's reason (not verified): " + SESSIONS.justification);
  await expect(consent).toContainText("Native app. When it claims the slot, you confirm it here or on Pairing requests");
  await expect(consent.getByText("harness pairing-requests arm test.example.native --scopes sessions", { exact: true })).toBeVisible();
  await consent.getByRole("button", { name: "Cancel" }).click();
  await expect(consent).toHaveCount(0);
  expect(writes).toEqual([]);
  await card(page, "test.example.native").getByRole("button", { name: "Set up Synthetic native" }).click();
  expect(writes).toEqual([]);
  await page.locator(".market-consent").getByRole("button", { name: "Approve" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({ path: "/api/marketplace/arm",
    body: { catalog_app_id: "test.example.native", scopes: ["sessions"] } });
  expect(writes[0].headers["x-hub-request"]).toBe("1");
  const handoff = page.locator(".market-handoff");
  await expect(handoff).toContainText("Give this only to Synthetic native; it lets that app claim the slot once.");
  await expect(handoff.getByText("pr-new", { exact: true })).toBeVisible();
  await expect(handoff.getByText("https://daemon.test", { exact: true })).toBeVisible();
  await expect(handoff.getByRole("button", { name: "Copy: pr-new" })).toBeVisible();
  await expect(card(page, "test.example.native").locator('[data-slot="pr-new"]')).toContainText("waiting for the app to claim");
  expect(outside).toEqual([]);
  expect(errors).toEqual([]);
});

test("the consent opens inside the entry's card and keeps its choices and focus across a poll (Hub design 10)", async ({ page }) => {
  await page.clock.install({ time: NOW * 1000 });
  const ops = entry("test.example.ops", { scopes: [SESSIONS, HOMELAB], elevated: ["homelab"],
    browser_origins: ["https://ops.example.test", "https://ops2.example.test"] });
  const { writes, errors } = await mount(page, view([ops, entry("test.example.native")]));
  // A calm info line, not a warn box; the entries under an uppercase header with their count; Troubleshooting last.
  await expect(page.locator(".banner.info")).toContainText("Being listed here grants nothing.");
  await expect(page.locator("main .notice.warn")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Entries 2" })).toBeVisible();
  expect(await page.locator("main > *").last().evaluate(el => el.matches("details.pairing-recovery"))).toBe(true);
  await expect(card(page, "test.example.ops").getByText("homelab · elevated", { exact: true })).toBeVisible();

  await card(page, "test.example.ops").getByRole("button", { name: "Set up Synthetic ops" }).click();
  const consent = card(page, "test.example.ops").getByRole("region", { name: "Set up Synthetic ops (test.example.ops)?" });
  await expect(consent).toBeVisible();
  await expect(consent.getByRole("heading", { level: 3 })).toBeFocused();
  await expect(page.locator(".market-consent")).toHaveCount(1);
  await expect(card(page, "test.example.ops").getByRole("button", { name: /^Set up/ })).toHaveCount(0);
  await expect(card(page, "test.example.native").getByRole("button", { name: "Set up Synthetic native" })).toBeVisible();
  await consent.getByRole("radio", { name: "https://ops2.example.test" }).check();
  await consent.getByRole("checkbox").check();

  await page.route("http://hub.test/api/marketplace", route => route.fulfill({ json: view([ops,
    entry("test.example.native"), entry("test.example.new")]) }));
  await page.clock.runFor(5500);
  await expect(card(page, "test.example.new")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Entries 3" })).toBeVisible();
  await expect(consent.getByRole("radio", { name: "https://ops2.example.test" })).toBeChecked();
  await expect(consent.getByRole("checkbox")).toBeChecked();
  await expect(consent.getByRole("checkbox")).toBeFocused();
  await expect(consent.getByRole("button", { name: "Approve" })).toBeEnabled();

  await consent.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".market-consent")).toHaveCount(0);
  await expect(card(page, "test.example.ops").getByRole("button", { name: "Set up Synthetic ops" })).toBeFocused();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test("a slot shows Pairing's countdown chip, and Withdraw asks inside its own slot", async ({ page }) => {
  await page.clock.install({ time: NOW * 1000 });
  const { writes, errors } = await mount(page, view([entry("test.example.native", { slots: [slot("pr-armed")] })]));
  const box = card(page, "test.example.native").locator('[data-slot="pr-armed"]');
  await expect(box.getByText("Waiting for the app", { exact: true })).toBeVisible();
  await expect(box.locator(".countdown")).toHaveText(/^[89]:\d\d left$/);
  const before = await box.locator(".countdown").textContent();
  await page.clock.runFor(2000);
  await expect(box.locator(".countdown")).not.toHaveText(before);
  await box.getByRole("button", { name: "Withdraw slot for Synthetic native" }).click();
  const ask = box.getByRole("alert");
  await expect(ask.getByRole("heading", { name: "Withdraw the slot for Synthetic native?" })).toBeVisible();
  await expect(ask.getByRole("button", { name: "Confirm withdraw" })).toBeFocused();
  await ask.getByRole("button", { name: "Cancel" }).click();
  await expect(box.getByRole("alert")).toHaveCount(0);
  await expect(box.getByRole("button", { name: "Withdraw slot for Synthetic native" })).toBeFocused();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test("the hand-off stays until Done even when its entry leaves the list after arming", async ({ page }) => {
  const armed = slot("pr-kept");
  const { writes, errors } = await mount(page, view([entry("test.example.native"), entry("test.example.other")]),
    { answer: () => ({ status: 201, json: { request: armed, daemon_url: "https://daemon.test", name: "Synthetic native" },
      view: view([entry("test.example.other")]) }) });
  await card(page, "test.example.native").getByRole("button", { name: /^Set up/ }).click();
  await page.locator(".market-consent").getByRole("button", { name: "Approve" }).click();
  await expect.poll(() => writes.length).toBe(1);
  const handoff = page.getByRole("region", { name: "Slot armed for Synthetic native" });
  await expect(handoff).toBeVisible();
  await expect(handoff.getByText("pr-kept", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Set up Synthetic native" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Entries 1" })).toBeVisible();
  await handoff.getByRole("button", { name: "Done" }).click();
  await expect(handoff).toHaveCount(0);
  await expect(page.locator("[data-entry]")).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("elevated scopes need the acknowledgement and a browser app needs its exact origin chosen", async ({ page }) => {
  const ops = entry("test.example.ops", { scopes: [SESSIONS, HOMELAB], elevated: ["homelab"],
    browser_origins: ["https://ops.example.test", "https://ops2.example.test"] });
  const { writes, errors } = await mount(page, view([ops, entry("test.example.web", {
    browser_origins: ["https://web.example.test"] })]), { answer: () => ({ status: 201, json: {
    request: slot("pr-ops", { origin: "https://ops2.example.test" }), daemon_url: "https://daemon.test", name: "Synthetic ops" } }) });
  await card(page, "test.example.ops").getByRole("button", { name: /^Set up/ }).click();
  const consent = page.locator(".market-consent");
  await expect(consent).toContainText("Claims and redemption must use the chosen exact origin");
  await expect(consent.locator('[data-scope="homelab"] .pill')).toHaveText("Elevated");
  await expect(consent.locator('[data-scope="homelab"] .disclosure-text'))
    .toHaveText("This app's tasks can read your homelab services' logs.");
  await expect(consent.locator('[data-scope="homelab"] .disclosure-text strong')).toHaveText("read your homelab services' logs");
  const approve = consent.getByRole("button", { name: "Approve" });
  await expect(approve).toBeDisabled();
  await consent.getByRole("checkbox").check();
  await expect(approve).toBeDisabled();
  await approve.click({ force: true });
  expect(writes).toEqual([]);
  await consent.getByRole("radio", { name: "https://ops2.example.test" }).check();
  await expect(approve).toBeEnabled();
  await expect(consent.getByText("harness pairing-requests arm test.example.ops --scopes sessions homelab " +
    "--origin https://ops2.example.test --acknowledge-elevated", { exact: true })).toBeVisible();
  await consent.getByRole("checkbox").uncheck();
  await expect(approve).toBeDisabled();
  await consent.getByRole("checkbox").check();
  await approve.click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body).toEqual({ catalog_app_id: "test.example.ops", scopes: ["sessions", "homelab"],
    origin: "https://ops2.example.test", acknowledge_elevated: true, consent_digest: "a".repeat(64) });
  await expect(page.locator(".market-handoff")).toContainText("The app must claim it from https://ops2.example.test");
  await expect(page.locator(".market-handoff")).toContainText("confirm before redemption");
  await expect(page.locator(".market-handoff")).not.toContainText("approves it at once");

  await page.locator(".market-handoff").getByRole("button", { name: "Done" }).click();
  await card(page, "test.example.web").getByRole("button", { name: /^Set up/ }).click();
  await expect(page.locator('.market-consent [data-field="origin"]')).toHaveText("https://web.example.test");
  await expect(page.locator(".market-consent")).toContainText("typed-code confirmation is also required");
  await expect(page.locator(".market-consent")).not.toContainText("no match code is needed");
  // The shared recovery steps sit in a closed Troubleshooting disclosure (Hub design 6).
  const recovery = page.locator("details.pairing-recovery");
  await recovery.getByText("Troubleshooting: the app says the slot was already claimed").click();
  await expect(recovery).toContainText("For a redeemed row, revoke its key_id. For an approved or claimed row, deny it.");
  for (const cli of ["harness pairing-requests list", "harness keys revoke <key_id>", "harness pairing-requests deny <id>"]) {
    await expect(recovery.getByText(cli, { exact: true })).toBeVisible();
  }
  await expect(page.locator(".market-consent").getByRole("checkbox")).toHaveCount(0);
  await expect(page.locator(".market-consent").getByRole("button", { name: "Approve" })).toBeEnabled();
  expect(errors).toEqual([]);
});

for (const [kind, origin] of [["native", ""], ["browser", "https://app.example.test"]]) {
  test(`${kind} claims need the typed code; slots are withdrawn after confirmation`, async ({ page }) => {
    const claimed = slot("pr-claimed", { state: "claimed", needs: "confirm", match_code: "482913",
      origin, browser: Boolean(origin) });
    const armed = slot("pr-armed");
    const { writes, errors } = await mount(page, view([entry("test.example.native", { slots: [claimed, armed],
      unverified_origin: { paired: [], pending_pairing: true } })]), { answer: (pathname, body) =>
      body.match === "000000" ? { status: 400, json: { detail: "the match code does not match the one the App shows" } } : null });
    const box = card(page, "test.example.native").locator('[data-slot="pr-claimed"]');
    // Even if a daemon answer carried a code, the page never shows it: the owner types what the app shows.
    expect(await box.evaluate(el => el.outerHTML.includes("482913"))).toBe(false);
    await expect(box.getByText("harness pairing-requests confirm pr-claimed --match <code>", { exact: true })).toBeVisible();
    const input = box.getByLabel("Match code the app shows");
    await expect(input).toHaveValue("");
    const confirm = box.getByRole("button", { name: "Confirm Synthetic native" });
    await expect(box).toContainText("Redemption is waiting for confirmation.");
    await expect(confirm).toBeDisabled();
    await input.fill("   ");
    await expect(confirm).toBeDisabled();
    expect(writes).toEqual([]);
    await input.fill("000000");
    await confirm.click();
    await expect(page.getByText("the match code does not match the one the App shows")).toBeVisible();
    expect(writes[0]).toMatchObject({ path: "/api/pairing/pr-claimed/confirm", body: { match: "000000" } });
    await card(page, "test.example.native").locator('[data-slot="pr-claimed"]').getByLabel("Match code the app shows").fill(" 482913 ");
    await card(page, "test.example.native").locator('[data-slot="pr-claimed"]').getByRole("button", { name: "Confirm Synthetic native" }).click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1]).toMatchObject({ path: "/api/pairing/pr-claimed/confirm", body: { match: "482913" } });
    await expect(page.getByText(/Confirmed Synthetic native/)).toBeVisible();

    const other = card(page, "test.example.native").locator('[data-slot="pr-armed"]');
    await expect(other.getByLabel("Match code the app shows")).toHaveCount(0);
    await other.getByRole("button", { name: "Withdraw slot for Synthetic native" }).click();
    await expect(page.getByRole("heading", { name: "Withdraw the slot for Synthetic native?" })).toBeVisible();
    expect(writes).toHaveLength(2);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(writes).toHaveLength(2);
    await other.getByRole("button", { name: "Withdraw slot for Synthetic native" }).click();
    await page.getByRole("button", { name: "Confirm withdraw", exact: true }).click();
    await expect.poll(() => writes.length).toBe(3);
    expect(writes[2]).toMatchObject({ path: "/api/pairing/pr-armed/deny", body: { confirm: true } });
    expect(errors).toEqual([]);
  });
}

test("a typed code survives a poll that changes the list", async ({ page }) => {
  await page.clock.install({ time: NOW * 1000 });
  const claimed = slot("pr-claimed", { state: "claimed", needs: "confirm" });
  const first = view([entry("test.example.native", { slots: [claimed] })]);
  await mount(page, first);
  const input = () => card(page, "test.example.native").getByLabel("Match code the app shows");
  await input().fill("4829");
  await page.route("http://hub.test/api/marketplace", route => route.fulfill({ json: view([
    entry("test.example.native", { slots: [claimed] }), entry("test.example.new")]) }));
  await page.clock.runFor(5500);
  await expect(card(page, "test.example.new")).toBeVisible();
  await expect(input()).toHaveValue("4829");
  await expect(input()).toBeFocused();
});

test("a named entries error keeps the last good entries; with none, no entries are shown", async ({ page }) => {
  const { errors } = await mount(page, view([entry("test.example.native")], { state: "error",
    error: "hub.entries: invalid JSON", stale: true }));
  await expect(page.getByRole("alert")).toContainText("hub.entries: invalid JSON");
  await expect(page.getByRole("alert")).toContainText("Showing the last good entries");
  await expect(card(page, "test.example.native")).toBeVisible();
  expect(errors).toEqual([]);

  const empty = await page.context().newPage();
  const second = await mount(empty, view(null, { state: "error", error: "entries[0].app.app_id: duplicate id",
    fetched_at: null }));
  await expect(empty.getByRole("alert")).toContainText("entries[0].app.app_id: duplicate id");
  await expect(empty.getByRole("alert")).toContainText("There are no last good entries to show.");
  await expect(empty.locator("[data-entry]")).toHaveCount(0);
  expect(second.errors).toEqual([]);
});

test("a 409 on Approve shows the server's reason and refreshes", async ({ page }) => {
  const { writes, errors } = await mount(page, view([entry("test.example.native")]), { answer: () => ({ status: 409,
    json: { detail: "The entry's scopes changed since the consent screen was shown. Review it again." } }) });
  await card(page, "test.example.native").getByRole("button", { name: /^Set up/ }).click();
  await page.locator(".market-consent").getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText(/The entry's scopes changed since the consent screen was shown/)).toBeVisible();
  await expect(page.locator(".market-consent")).toHaveCount(0);
  expect(writes).toHaveLength(1);
  expect(errors).toEqual([]);
});

test("entry text renders as text, never as HTML", async ({ page }) => {
  const markup = '<img src=x onerror="window.injected=1"><script>window.injected=2</script>';
  const { errors, outside } = await mount(page, view([entry("test.example.x", { name: markup, summary: markup,
    publisher: { name: markup, id: "pub-0a1b2c3d4e5f" }, scopes: [{ ...SESSIONS, text: markup, justification: markup }] })]));
  await expect(card(page, "test.example.x").locator("h2")).toHaveText(markup);
  await card(page, "test.example.x").getByRole("button", { name: /^Set up/ }).click();
  await expect(page.locator(".market-consent .disclosure-text")).toHaveText(markup);
  await expect(page.locator("main img, main script")).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(outside).toEqual([]);
  expect(errors).toEqual([]);
});

for (const [label, width] of [["phone", 390], ["desktop", 1280]]) {
  test(`${label} layout keeps 44px targets and no horizontal scroll`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const long = "https://a-very-long-subdomain-name-for-layout.example.test";
    const { errors } = await mount(page, view([
      entry("test.example.ops", { scopes: [SESSIONS, { ...HOMELAB, text: "A long disclosure ".repeat(20) }],
        elevated: ["homelab"], browser_origins: [long, long + ":8443"], icon: ICON }),
      entry("test.example.native", { slots: [slot("pr-c", { state: "claimed", needs: "confirm" })],
        unverified_origin: { paired: ["k-native"], pending_pairing: true } })]));
    await card(page, "test.example.ops").getByRole("button", { name: /^Set up/ }).click();
    // Host commands shown, then hidden behind their Host command buttons: every target is measured either way.
    for (const shown of [true, false]) {
      await page.evaluate(on => document.body.classList.toggle("show-host-commands", on), shown);
      for (const target of await page.locator("main button:visible, main input:visible, main summary:visible").all()) {
        const box = await target.boundingBox();
        expect(box.height, await target.evaluate(el => el.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(44);
        expect(box.width).toBeGreaterThanOrEqual(44);
      }
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}


test("disclosure drift reopens daemon consent and sends its digest only after a new approval", async ({ page }) => {
  const original = entry("test.example.ops", { browser_origins: ["https://ops.example.test"] });
  const daemon = entry("test.example.ops", { ...original, consent_digest: "b".repeat(64),
    scopes: [{ ...SESSIONS, tier: "elevated", text: "New daemon description for all project files." }],
    elevated: ["sessions"] });
  let attempts = 0;
  const { writes, errors } = await mount(page, view([original]), { answer: () => ++attempts === 1 ? {
    status: 409, json: { detail: "The daemon describes these scopes differently from what you saw. Review the consent screen again.",
      consent_entry: daemon }, view: view([daemon]) } : { status: 201,
    json: { request: slot("pr-new"), name: "Synthetic ops", daemon_url: "https://daemon.test" } } });
  await card(page, original.catalog_app_id).getByRole("button", { name: /^Set up/ }).click();
  await page.locator(".market-consent").getByRole("button", { name: "Approve" }).click();
  const consent = page.locator(".market-consent");
  await expect(consent.locator(".disclosure-text")).toHaveText(daemon.scopes[0].text);
  await expect(consent.getByRole("heading", { level: 3 })).toContainText(original.catalog_app_id);
  await expect(page.locator(".market-handoff")).toHaveCount(0);
  await expect(consent.getByRole("checkbox")).not.toBeChecked();
  await expect(consent.getByRole("button", { name: "Approve" })).toBeDisabled();
  expect(writes).toHaveLength(1);
  expect(writes[0].body.consent_digest).toBe(original.consent_digest);
  await consent.getByRole("checkbox").check();
  await consent.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".market-handoff")).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toMatchObject({ consent_digest: daemon.consent_digest, acknowledge_elevated: true });
  expect(errors).toEqual([]);
});


test("blank add-on copy names the Hub and requires a separate acknowledgement", async ({ page }) => {
  const addon = entry("test.example.files", { scopes: [{ scope: "files:write", tier: "standard", text: "",
    justification: "Publisher's reason" }] });
  const { writes, errors } = await mount(page, view([addon]));
  await card(page, addon.catalog_app_id).getByRole("button", { name: /^Set up/ }).click();
  const consent = page.locator(".market-consent");
  await expect(consent.locator(".disclosure-text")).toHaveText(
    "The Hub has no description for this scope. The daemon's description appears once the slot is armed.");
  await expect(consent.getByRole("button", { name: "Approve" })).toBeDisabled();
  await consent.getByRole("checkbox").check();
  await expect(consent.getByRole("button", { name: "Approve" })).toBeEnabled();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

for (const state of ["approved", "redeemed"]) {
  test(`a browser claim that skips confirmation warns even when already ${state}`, async ({ page }) => {
    await page.clock.install({ time: NOW * 1000 });
    const browser = slot("pr-legacy", { browser: true, origin: "https://web.example.test" });
    const { writes, errors } = await mount(page, view([entry("test.example.web", { slots: [browser] })]));
    const warning = "This daemon approved the claim without confirmation; update the daemon.";
    await page.route("http://hub.test/api/marketplace", route => route.fulfill({ json: view([
      entry("test.example.web", { slots: state === "redeemed" ? [] : [{ ...browser, state, needs: "redeem" }] })
    ], { confirmation_warning: warning }) }));
    await page.clock.runFor(5500);
    await expect(page.getByRole("alert")).toHaveText(warning);
    await page.getByRole("button", { name: "Refresh entries" }).click();
    await expect(page.getByRole("alert")).toHaveText(warning);
    expect(await page.locator("main").evaluate(el => el.outerHTML.includes("482913"))).toBe(false);
    expect(writes).toEqual([]);
    expect(errors).toEqual([]);
  });
}
