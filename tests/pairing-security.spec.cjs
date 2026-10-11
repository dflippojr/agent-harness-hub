// Run: playwright test --config=tests/playwright.config.cjs --workers=1 pairing-security
// Synthetic intercepted Hub responses: no daemon, Hub server or real account is used.
const { test, expect } = require("playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");

const NOW = 1700000000;
const request = (id, extra = {}) => ({
  id, kind: "app", state: "pending", needs: "approve", name: "Synthetic " + id, catalog_app_id: "test.example.notes",
  origin: "https://app.test", browser: true, armed: false, scopes: ["sessions"], elevated: [],
  disclosures: [{ scope: "sessions", tier: "standard", text: "This app can start agent tasks." }],
  match_code: "482913", key_id: "", created_at: NOW - 60, expires_at: NOW + 540, approved_at: null, finished_at: null,
  ...extra,
});

async function mount(page, initial, { answer } = {}) {
  const writes = [], errors = [];
  let rows = initial;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://hub.test/**", async route => {
    const req = route.request(), pathname = new URL(req.url()).pathname;
    if (pathname.startsWith("/api/")) {
      if (pathname === "/api/pairing" && req.method() === "GET") {
        return route.fulfill({ json: { requests: rows, now: NOW } });
      }
      const match = /^\/api\/pairing\/([^/]+)\/(approve|confirm|deny)$/.exec(pathname);
      if (!match || req.method() !== "POST") throw Error("Unexpected API call: " + req.method() + " " + pathname);
      writes.push({ path: pathname, body: req.postDataJSON(), headers: req.headers() });
      const reply = answer ? answer(match[1], match[2], req.postDataJSON()) : null;
      if (reply) {
        if (reply.rows) rows = reply.rows;
        return route.fulfill({ status: reply.status, json: reply.json });
      }
      const row = rows.find(r => r.id === match[1]);
      rows = rows.map(r => r.id === match[1] ? { ...r, state: match[2] === "deny" ? "denied" : "approved",
        needs: match[2] === "deny" ? "" : "redeem", match_code: "" } : r);
      return route.fulfill({ json: { ...row, state: "approved" } });
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<link rel="stylesheet" href="/static/style.css"><main id="main" class="main"></main>' +
      '<script type="module">import {renderPairing} from "/static/pages/pairing.mjs"; renderPairing(document.querySelector("main"));</script>' });
    if (!/^\/static\/[a-z/.-]+\.(mjs|css)$/.test(pathname)) throw Error("Unexpected resource: " + pathname);
    return route.fulfill({ contentType: pathname.endsWith(".css") ? "text/css" : "text/javascript",
      body: await readFile(path.join(__dirname, "../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.locator("[data-request-id]")).toHaveCount(initial.length);
  return { writes, errors };
}
const card = (page, id) => page.locator('[data-request-id="' + id + '"]');

test("approve needs the typed code; it is never pre-filled and a wrong code shows the daemon's 400", async ({ page }) => {
  const { writes, errors } = await mount(page, [request("pr-a")], { answer: (id, action, body) =>
    body.match === "000000" ? { status: 400, json: { detail: "the match code does not match the one the App shows" } } : null });
  const box = card(page, "pr-a");
  await expect(box).not.toContainText("482913");
  await expect(box.locator(".match")).toHaveCount(0);
  await expect(box).toContainText("https://app.test");
  await expect(box).toContainText("test.example.notes");
  await expect(box).toContainText(/\d+:\d\d left/);
  await expect(box).toContainText("Type the code the app is showing right now.");
  await expect(box.getByText("harness pairing-requests approve pr-a --match <code>", { exact: true })).toBeVisible();
  const input = box.getByLabel("Match code the app shows");
  await expect(input).toHaveValue("");
  const approve = box.getByRole("button", { name: /^Approve/ });
  await expect(approve).toBeDisabled();
  await input.fill("000000");
  await approve.click();
  await expect(page.getByText("the match code does not match the one the App shows")).toBeVisible();
  expect(writes[0].body).toEqual({ match: "000000" });
  expect(writes[0].headers["x-hub-request"]).toBe("1");
  await card(page, "pr-a").getByLabel("Match code the app shows").fill("482913");
  await card(page, "pr-a").getByRole("button", { name: /^Approve/ }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({ path: "/api/pairing/pr-a/approve", body: { match: "482913" } });
  await expect(page.locator('[data-state="approved"] [data-request-id="pr-a"]')).toBeVisible();
  expect(errors).toEqual([]);
});

test("elevated scopes keep Approve disabled until the separate acknowledgement is ticked", async ({ page }) => {
  const row = request("pr-e", { scopes: ["sessions", "approvals"], elevated: ["approvals"], disclosures: [
    { scope: "sessions", tier: "standard", text: "This app can start agent tasks." },
    { scope: "approvals", tier: "elevated", text: "This app can **approve or deny tool calls** in its own tasks." }] });
  const { writes, errors } = await mount(page, [row]);
  const box = card(page, "pr-e");
  await expect(box.locator('[data-scope="approvals"] .pill')).toHaveText("elevated");
  await expect(box.locator('[data-scope="approvals"] .disclosure-text'))
    .toHaveText("This app can **approve or deny tool calls** in its own tasks.");
  await expect(box.getByText("harness pairing-requests approve pr-e --match <code> --acknowledge-elevated", { exact: true })).toBeVisible();
  await box.getByLabel("Match code the app shows").fill("482913");
  const approve = box.getByRole("button", { name: /^Approve/ });
  await expect(approve).toBeDisabled();
  await approve.click({ force: true });
  expect(writes).toHaveLength(0);
  await box.getByRole("checkbox").check();
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body).toEqual({ match: "482913", acknowledge_elevated: true });
  expect(errors).toEqual([]);
});

test("disclosure text and names render as text, never as HTML", async ({ page }) => {
  const markup = '<img src=x onerror="window.injected=1"><script>window.injected=2</script>';
  const { errors } = await mount(page, [request("pr-x", { name: markup, origin: "", browser: false,
    disclosures: [{ scope: "sessions", tier: "standard", text: markup }] })]);
  await expect(card(page, "pr-x").locator(".disclosure-text")).toHaveText(markup);
  await expect(card(page, "pr-x").locator("h3")).toHaveText(markup + " · pr-x");
  await expect(card(page, "pr-x")).toContainText("native app");
  await expect(page.locator("main img, main script")).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(errors).toEqual([]);
});

test("confirm a native claim, deny after an in-page confirmation, and Hub claims offer only the host command", async ({ page }) => {
  const rows = [request("pr-c", { state: "claimed", needs: "confirm", origin: "", browser: false, armed: true, match_code: "111222" }),
    request("pr-d"), request("pr-s", { state: "armed", needs: "claim", match_code: "", armed: true }),
    request("pr-h", { kind: "hub", name: "Synthetic Hub", scopes: ["admin"], elevated: ["admin"], match_code: "",
      disclosures: [{ scope: "admin", tier: "elevated", text: "This is a **Hub claim**." }] }),
    request("pr-r", { state: "redeemed", needs: "", match_code: "", key_id: "k-synthetic", finished_at: NOW - 5 })];
  const { writes, errors } = await mount(page, rows);
  await card(page, "pr-c").getByLabel("Match code the app shows").fill("111222");
  await card(page, "pr-c").getByRole("button", { name: /^Confirm/ }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({ path: "/api/pairing/pr-c/confirm", body: { match: "111222" } });
  await card(page, "pr-d").getByRole("button", { name: /^Deny/ }).click();
  await expect(page.getByRole("heading", { name: "Deny Synthetic pr-d?" })).toBeVisible();
  expect(writes).toHaveLength(1);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(writes).toHaveLength(1);
  await card(page, "pr-d").getByRole("button", { name: /^Deny/ }).click();
  await page.getByRole("button", { name: "Confirm deny", exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({ path: "/api/pairing/pr-d/deny", body: { confirm: true } });
  await expect(card(page, "pr-s").getByRole("button", { name: /^Withdraw/ })).toBeVisible();
  await expect(card(page, "pr-s").getByLabel("Match code the app shows")).toHaveCount(0);
  const hub = card(page, "pr-h");
  await expect(hub.locator("button:not(.copy)")).toHaveCount(0);
  await expect(hub.locator("input")).toHaveCount(0);
  await expect(hub.getByText("harness hub approve pr-h --match <code>", { exact: true })).toBeVisible();
  await expect(card(page, "pr-r").locator("button:not(.copy), input")).toHaveCount(0);
  await expect(card(page, "pr-r")).toContainText("k-synthetic");
  expect(errors).toEqual([]);
});

test("409 and 404 answers refresh the list with a short note", async ({ page }) => {
  const expired = request("pr-n", { state: "expired", needs: "", match_code: "" });
  const { errors } = await mount(page, [request("pr-n"), request("pr-g")], { answer: (id) => id === "pr-n" ?
    { status: 409, rows: [expired, request("pr-g")], json: { detail: "this pairing request is expired" } } :
    { status: 404, rows: [expired], json: { detail: "no pairing request has that id" } } });
  await card(page, "pr-n").getByLabel("Match code the app shows").fill("482913");
  await card(page, "pr-n").getByRole("button", { name: /^Approve/ }).click();
  await expect(page.getByText(/changed state before your decision/)).toBeVisible();
  await expect(page.locator('[data-state="expired"] [data-request-id="pr-n"]')).toBeVisible();
  await card(page, "pr-g").getByRole("button", { name: /^Deny/ }).click();
  await page.getByRole("button", { name: "Confirm deny", exact: true }).click();
  await expect(page.getByText("That request no longer exists. The list was refreshed.")).toBeVisible();
  await expect(card(page, "pr-g")).toHaveCount(0);
  expect(errors).toEqual([]);
});

for (const [label, width] of [["phone", 390], ["desktop", 1280]]) {
  test(`${label} layout keeps 44px targets and no horizontal scroll`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const row = request("pr-l", { scopes: ["sessions", "homelab"], elevated: ["homelab"], disclosures: [
      { scope: "homelab", tier: "elevated", text: "A long disclosure ".repeat(20) }] });
    const { errors } = await mount(page, [row, request("pr-m", { state: "armed", needs: "claim", match_code: "" })]);
    for (const target of await page.locator("main button, main input").all()) {
      const box = await target.boundingBox();
      expect(box.height, await target.evaluate(el => el.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(44);
      expect(box.width).toBeGreaterThanOrEqual(44);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}


for (const [state, needs] of [["pending", "approve"], ["claimed", "confirm"]]) {
  test(`${state} cards never expose the supplied code before the owner types it`, async ({ page }) => {
    const row = request("pr-hidden", { state, needs, armed: state === "claimed", origin: "", browser: false });
    const { errors } = await mount(page, [row]);
    const box = card(page, row.id);
    await expect(box).not.toContainText(row.match_code);
    expect(await box.evaluate((el, code) => el.outerHTML.includes(code), row.match_code)).toBe(false);
    await expect(box.getByLabel("Match code the app shows")).toHaveValue("");
    await expect(box).toContainText("If the app shows no code or an error, deny.");
    if (state === "claimed") {
      await expect(box).toContainText("If your app reported that the slot was already claimed, withdraw this.");
    }
    expect(errors).toEqual([]);
  });
}


test("requester metadata is unverified and owner-armed metadata explains its source", async ({ page }) => {
  const { errors } = await mount(page, [request("pr-app"),
    request("pr-owner", { state: "claimed", needs: "confirm", armed: true })]);
  const app = card(page, "pr-app"), owner = card(page, "pr-owner");
  await expect(app.locator("dt").filter({ hasText: "stated by the requester, not verified" })).toHaveCount(2);
  await expect(app.locator("dt").filter({ hasText: /^Started from$/ }).locator("+ dd")).toHaveText("the app");
  await expect(app).toContainText("only the match code on your app's screen proves this is your app");
  await expect(owner.locator("dt").filter({ hasText: /^Started from$/ }).locator("+ dd")).toHaveText("this Hub");
  await expect(owner).toContainText("You set the origin and catalog app id when you armed this slot.");
  await expect(owner).not.toContainText("stated by the requester, not verified");
  expect(errors).toEqual([]);
});
