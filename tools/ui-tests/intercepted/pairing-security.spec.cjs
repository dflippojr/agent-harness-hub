// Run: npm --prefix tools/ui-tests test -- --project=intercepted
// Synthetic intercepted Hub responses: no daemon, Hub server or real account is used.
const { test, expect } = require("@playwright/test");
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

async function mount(page, initial, { answer, read } = {}) {
  const writes = [], errors = [];
  let rows = initial;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://hub.test/**", async route => {
    const req = route.request(), pathname = new URL(req.url()).pathname;
    if (pathname.startsWith("/api/")) {
      if (pathname === "/api/pairing" && req.method() === "GET") {
        const reply = read ? read() : null;
        if (reply === "abort") return route.abort("connectionrefused");
        if (reply) return route.fulfill(reply);
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
      body: await readFile(path.join(__dirname, "../../../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.locator("[data-request-id]")).toHaveCount(initial.length);
  // These regressions read each action's CLI equivalent, so the Show host commands switch is on (Hub design 3, #70).
  await page.evaluate(() => document.body.classList.add("show-host-commands"));
  return { writes, errors };
}
const card = (page, id) => page.locator('[data-request-id="' + id + '"]');

for (const [kind, origin] of [["browser", "https://app.test"], ["native", ""]]) {
  test(`${kind} claimed slots wait for the typed code and refuse wrong codes`, async ({ page }) => {
    const row = request("pr-confirm", { state: "claimed", needs: "confirm", armed: true,
      origin, browser: Boolean(origin), match_code: "" });
    const { writes, errors } = await mount(page, [row], { answer: (id, action, body) =>
      body.match === "000000" ? { status: 400, json: { detail: "the match code does not match the one the App shows",
        error: { code: "match_mismatch" } } } : null });
    const box = card(page, row.id), input = box.getByLabel("Match code the app shows");
    const confirm = box.getByRole("button", { name: /^Confirm/ });
    await expect(box).toContainText("Redemption is waiting for confirmation");
    await expect(box).not.toContainText("A native app claimed");
    await expect(input).toHaveValue("");
    await expect(confirm).toBeDisabled();
    await input.fill("   ");
    await expect(confirm).toBeDisabled();
    expect(writes).toEqual([]);
    await input.fill("000000");
    await confirm.click();
    await expect(page.getByText("the match code does not match the one the App shows")).toBeVisible();
    await expect(box).toContainText("Redemption is waiting for confirmation");
    await expect(box).not.toContainText("482913");
    await input.fill(" 482913 ");
    await confirm.click();
    await expect(page.locator('[data-group="waiting"] [data-request-id="pr-confirm"][data-state="approved"]')).toBeVisible();
    expect(writes.map(w => w.body)).toEqual([{ match: "000000" }, { match: "482913" }]);
    expect(writes.every(w => w.path === "/api/pairing/pr-confirm/confirm")).toBe(true);
    await expect(box.getByLabel("Match code the app shows")).toHaveCount(0);
    await expect(box).toContainText("Waiting for the app to fetch its own token");
    expect(errors).toEqual([]);
  });
}

test("already-claimed recovery distinguishes redeemed keys from unredeemed claims", async ({ page }) => {
  await mount(page, [request("pr-redeemed", { state: "redeemed", needs: "", key_id: "k-recovery", match_code: "",
    finished_at: NOW - 5 })]);
  // Troubleshooting is a closed disclosure at the foot of the page until the owner opens it.
  const recovery = page.locator("details.pairing-recovery");
  await expect(recovery.getByText("harness pairing-requests list", { exact: true })).toBeHidden();
  await recovery.getByText("Troubleshooting: the app says the slot was already claimed").click();
  await expect(recovery).toContainText("For a redeemed row, revoke its key_id. For an approved or claimed row, deny it.");
  for (const cli of ["harness pairing-requests list", "harness keys revoke <key_id>", "harness pairing-requests deny <id>"]) {
    await expect(recovery.getByText(cli, { exact: true })).toBeVisible();
  }
  // The redeemed row is a History row naming its key, which Apps (or harness keys revoke) can revoke.
  await expect(card(page, "pr-redeemed").locator("td")).toHaveText(
    ["Synthetic pr-redeemed", "App", "Paired", "sessions", /\d/, "k-recovery"]);
});

test("elevated scopes keep Approve disabled until the separate acknowledgement is ticked", async ({ page }) => {
  const row = request("pr-e", { scopes: ["sessions", "approvals"], elevated: ["approvals"], disclosures: [
    { scope: "sessions", tier: "standard", text: "This app can start agent tasks." },
    { scope: "approvals", tier: "elevated", text: "This app can **approve or deny tool calls** in its own tasks." }] });
  const { writes, errors } = await mount(page, [row]);
  const box = card(page, "pr-e");
  await expect(box.locator('[data-scope="approvals"] .pill')).toHaveText("Elevated");
  await expect(box.locator(".decision-head .pill")).toHaveText(["Needs approval", "Elevated scope"]);
  await expect(box.locator('[data-scope="approvals"] .disclosure-text'))
    .toHaveText("This app can approve or deny tool calls in its own tasks.");
  await expect(box.locator('[data-scope="approvals"] .disclosure-text strong')).toHaveText("approve or deny tool calls");
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
  await expect(card(page, "pr-x").locator(".disclosure-text strong")).toHaveCount(0);
  await expect(card(page, "pr-x").locator("h3")).toHaveText(markup);
  await expect(card(page, "pr-x")).toContainText("Native app · catalog test.example.notes");
  await expect(page.locator("main img, main script")).toHaveCount(0);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(errors).toEqual([]);
});

test("only **pairs** in disclosures become bold, and markup inside them stays text", async ({ page }) => {
  const markup = '<img src=x onerror="window.injected=1">';
  const { errors } = await mount(page, [request("pr-b", { disclosures: [{ scope: "sessions", tier: "standard",
    text: "Plain **" + markup + "** and *single* and ** unmatched" }] })]);
  const text = card(page, "pr-b").locator(".disclosure-text");
  await expect(text).toHaveText("Plain " + markup + " and *single* and ** unmatched");
  await expect(text.locator("strong")).toHaveText(markup);
  await expect(page.locator("main img")).toHaveCount(0);
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
  // Its card has moved to History, so the note stands under the page head instead.
  await expect(page.locator("main > p.notice[aria-live]")).toContainText("changed state before your decision");
  await expect(page.locator('[data-group="history"] [data-request-id="pr-n"][data-state="expired"]')).toBeVisible();
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
    // Host commands shown, then hidden behind their Host command buttons: every target is measured either way.
    for (const shown of [true, false]) {
      await page.evaluate(on => document.body.classList.toggle("show-host-commands", on), shown);
      for (const target of await page.locator("main button:visible, main input:visible").all()) {
        const box = await target.boundingBox();
        expect(box.height, await target.evaluate(el => el.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(44);
        expect(box.width).toBeGreaterThanOrEqual(44);
      }
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
      await expect(box).toContainText("Redemption is waiting for confirmation");
    }
    expect(errors).toEqual([]);
  });
}


test("requester metadata is unverified and owner-armed metadata explains its source", async ({ page }) => {
  const { errors } = await mount(page, [request("pr-app"),
    request("pr-owner", { state: "claimed", needs: "confirm", armed: true })]);
  const app = card(page, "pr-app"), owner = card(page, "pr-owner");
  // Said once per card, not inside every label.
  await expect(app.locator(".caveat")).toHaveText("Name, origin and catalog id are stated by the app, not verified. " +
    "Only the match code on your app's screen proves this is your app.");
  await expect(app.locator("dt").filter({ hasText: /^Started from$/ }).locator("+ dd")).toHaveText(/^The app · /);
  await expect(app.locator("dt").filter({ hasText: /^From$/ }).locator("+ dd"))
    .toHaveText("https://app.test · catalog test.example.notes");
  await expect(owner.locator("dt").filter({ hasText: /^Bound to$/ }).locator("+ dd"))
    .toHaveText("https://app.test · catalog test.example.notes (set by you)");
  await expect(owner.locator(".caveat")).toContainText("You armed this slot and set its origin and catalog app id");
  await expect(owner).not.toContainText("not verified");
  expect(errors).toEqual([]);
});


// One field is enough: the withheld check is one code path over name, origin and catalog_app_id. The server marks
// each in test_pairing.py::test_withheld_pairing_identity_fields_are_marked_for_host_review.
for (const [state, needs] of [["pending", "approve"], ["claimed", "confirm"]]) {
  test(`a withheld name prevents ${needs} but keeps Deny`, async ({ page }) => {
    const row = request("pr-withheld", { name: "[withheld: token-shaped]", state, needs, armed: state === "claimed" });
    const { writes, errors } = await mount(page, [row]);
    const box = card(page, row.id);
    await expect(box.locator('[data-action="approve"], [data-action="confirm"]')).toHaveCount(0);
    await expect(box.locator("input")).toHaveCount(0);
    await expect(box).toContainText("Review this request with harness pairing-requests list on the host.");
    await expect(box.getByText("harness pairing-requests list", { exact: true })).toBeVisible();
    await box.locator('[data-action="deny"]').click();
    await page.getByRole("button", { name: "Confirm deny", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toMatchObject({ path: "/api/pairing/pr-withheld/deny", body: { confirm: true } });
    expect(errors).toEqual([]);
  });
}

test("a daemon confirmation warning appears on Pairing requests after a poll", async ({ page }) => {
  await page.clock.install({ time: NOW * 1000 });
  const browser = request("pr-legacy", { state: "armed", armed: true, needs: "claim" });
  const { writes, errors } = await mount(page, [browser]);
  const warning = "This daemon approved the claim without confirmation; update the daemon.";
  await page.route("http://hub.test/api/pairing", route => route.fulfill({ json: {
    requests: [{ ...browser, state: "redeemed", needs: "", key_id: "k-legacy" }], now: NOW,
    confirmation_warning: warning,
  } }));
  await page.clock.runFor(5500);
  await expect(page.getByRole("alert")).toHaveText(warning);
  expect(await page.locator("main").evaluate(el => el.outerHTML.includes("482913"))).toBe(false);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test("a refused approve below the fold brings the message into view and focuses it", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const rows = ["pr-1", "pr-2", "pr-3", "pr-4", "pr-5", "pr-6"].map(id => request(id));
  const { errors } = await mount(page, rows, { answer: (id, action, body) => body.match === "000000" ?
    { status: 400, json: { detail: "the match code does not match the one the App shows",
      error: { code: "match_mismatch" } } } : null });
  const box = card(page, "pr-6");
  await box.scrollIntoViewIfNeeded();
  await box.getByLabel("Match code the app shows").fill("000000");
  await box.getByRole("button", { name: /^Approve/ }).click();
  const note = page.getByText("The match code does not match the one the App shows.", { exact: true });
  // Shown in the card that was acted on (decision 5 of #65), as a sentence.
  await expect(box.locator("p.notice.bad")).toHaveText("The match code does not match the one the App shows.");
  await expect(note).toBeFocused();
  await expect(note).toBeInViewport();
  expect(await page.evaluate(() => document.activeElement !== document.body)).toBe(true);
  expect(errors).toEqual([]);
});


test("requests group into Needs you, Waiting for the app and History, each with its count", async ({ page }) => {
  const { errors } = await mount(page, [request("pr-p"), request("pr-c", { state: "claimed", needs: "confirm", armed: true }),
    request("pr-a", { state: "approved", needs: "redeem" }), request("pr-s", { state: "armed", needs: "claim", armed: true }),
    request("pr-r", { state: "redeemed", needs: "", key_id: "k-synthetic", finished_at: NOW - 5 }),
    request("pr-d", { state: "denied", needs: "", finished_at: NOW - 50 }),
    request("pr-x", { state: "expired", needs: "", finished_at: NOW - 90 })]);
  await expect(page.locator(".pairing-group .section-head")).toHaveText(["Needs you2", "Waiting for the app2", "History · last day3"]);
  await expect(page.locator('[data-group="needs"] [data-request-id]')).toHaveCount(2);
  await expect(card(page, "pr-p").locator(".decision-head .pill")).toHaveText("Needs approval");
  await expect(card(page, "pr-c").locator(".decision-head .pill")).toHaveText("Needs confirmation");
  await expect(card(page, "pr-a").locator(".pill")).toHaveText("Approved");
  await expect(card(page, "pr-s").locator(".pill")).toHaveText("Waiting for the app");
  await expect(card(page, "pr-s")).toContainText("Waiting for the app to claim this slot · sessions");
  await expect(card(page, "pr-a")).toContainText(/\d+:\d\d left/);
  const history = page.getByRole("table", { name: "Finished pairing requests" });
  await expect(history.locator("thead th")).toHaveText(["Name", "Kind", "State", "Scopes", "Finished", "Key"]);
  await expect(history.locator("tbody tr .pill")).toHaveText(["Paired", "Denied", "Expired"]);
  await expect(card(page, "pr-r")).toContainText("Paired");
  await expect(card(page, "pr-d")).toContainText("Denied");
  expect(errors).toEqual([]);
});

test("the countdown chip ticks and turns warn in the last two minutes", async ({ page }) => {
  await page.clock.install({ time: NOW * 1000 });
  const { errors } = await mount(page, [request("pr-t", { expires_at: NOW + 300 }),
    request("pr-u", { expires_at: NOW + 110 }), request("pr-v", { expires_at: NOW - 1 })]);
  const later = card(page, "pr-t").locator(".countdown");
  await expect(later).toHaveText(/^[45]:\d\d left$/);
  await expect(later).not.toHaveClass(/\bwarn\b/);
  await expect(card(page, "pr-u").locator(".countdown")).toHaveText(/^1:\d\d left$/);
  await expect(card(page, "pr-u").locator(".countdown")).toHaveClass(/\bwarn\b/);
  await expect(card(page, "pr-v").locator(".countdown")).toHaveText("Expired");
  // Between polls the chip counts down every second on its own.
  const before = await later.textContent();
  await page.clock.runFor(2000);
  await expect(later).not.toHaveText(before);
  expect(errors).toEqual([]);
});

test("deny asks inside the card that was clicked, survives a poll, and Cancel returns to Deny", async ({ page }) => {
  await page.clock.install({ time: NOW * 1000 });
  const { writes, errors } = await mount(page, [request("pr-1"), request("pr-2")]);
  await card(page, "pr-2").getByLabel("Match code the app shows").fill("12");
  await card(page, "pr-2").getByRole("button", { name: "Deny Synthetic pr-2" }).click();
  const confirm = card(page, "pr-2").getByRole("alert");
  await expect(confirm.getByRole("heading", { name: "Deny Synthetic pr-2?" })).toBeVisible();
  await expect(confirm).toContainText("Deny pairing request pr-2 from https://app.test.");
  await expect(confirm.getByRole("button", { name: "Confirm deny" })).toBeFocused();
  await expect(card(page, "pr-1").getByRole("alert")).toHaveCount(0);
  // A poll that changes the list redraws the cards; the question stays where it was asked.
  await page.route("http://hub.test/api/pairing", route => route.fulfill({ json: {
    requests: [request("pr-1"), request("pr-2"), request("pr-3")], now: NOW } }));
  await page.clock.runFor(5500);
  await expect(card(page, "pr-3")).toBeVisible();
  await expect(card(page, "pr-2").getByRole("button", { name: "Confirm deny" })).toBeVisible();
  await card(page, "pr-2").getByRole("button", { name: "Cancel" }).click();
  await expect(card(page, "pr-2").getByRole("alert")).toHaveCount(0);
  await expect(card(page, "pr-2").getByRole("button", { name: "Deny Synthetic pr-2" })).toBeFocused();
  await expect(card(page, "pr-2").getByLabel("Match code the app shows")).toHaveValue("12");
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test("Withdraw asks inside the waiting row", async ({ page }) => {
  const { writes, errors } = await mount(page, [request("pr-w", { state: "armed", needs: "claim", armed: true, origin: "",
    browser: false })]);
  await card(page, "pr-w").getByRole("button", { name: "Withdraw Synthetic pr-w" }).click();
  const confirm = card(page, "pr-w").getByRole("alert");
  await expect(confirm).toContainText("Withdraw slot pr-w from a native app. The app cannot use it afterwards; it must ask again.");
  await expect(card(page, "pr-w").getByRole("button", { name: /^Withdraw/ })).toHaveCount(0);
  await confirm.getByRole("button", { name: "Confirm withdraw" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({ path: "/api/pairing/pr-w/deny", body: { confirm: true } });
  await expect(page.locator('[data-group="history"] [data-request-id="pr-w"][data-state="denied"]')).toBeVisible();
  await expect(page.getByText("Withdrew Synthetic pr-w.")).toBeVisible();
  expect(errors).toEqual([]);
});

test("with no requests the page says how one appears", async ({ page }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://hub.test/**", async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/pairing") return route.fulfill({ json: { requests: [], now: NOW } });
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<link rel="stylesheet" href="/static/style.css"><main id="main" class="main"></main>' +
      '<script type="module">import {renderPairing} from "/static/pages/pairing.mjs"; renderPairing(document.querySelector("main"));</script>' });
    return route.fulfill({ contentType: pathname.endsWith(".css") ? "text/css" : "text/javascript",
      body: await readFile(path.join(__dirname, "../../../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.getByRole("heading", { name: "No pairing requests in the last day" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open Marketplace" })).toHaveAttribute("href", "#/marketplace");
  await expect(page.locator(".pairing-group")).toHaveCount(0);
  expect(errors).toEqual([]);
});

for (const [state, needs, armed] of [["pending", "approve", false], ["claimed", "confirm", true]]) {
  test(`a ${state} card lists every requested scope, also one the daemon did not describe`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const { errors } = await mount(page, [request("pr-s", { state, needs, armed, scopes: ["sessions", "images"],
      disclosures: [{ scope: "sessions", tier: "standard", text: "This app can start agent tasks." }] })]);
    const scopes = card(page, "pr-s").locator("dt").filter({ hasText: /^Scopes$/ }).locator("+ dd");
    await expect(scopes).toBeVisible();
    await expect(scopes.locator(".scope-chip")).toHaveText(["Standard sessions", "Standard images"]);
    expect(errors).toEqual([]);
  });
}

test("a claimed slot keeps the daemon's scope text one tap away", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { errors } = await mount(page, [request("pr-k", { state: "claimed", needs: "confirm", armed: true,
    scopes: ["sessions:all"], elevated: ["sessions:all"], disclosures: [{ scope: "sessions:all", tier: "elevated",
      text: "This app can **read every session on this harness**." }] })]);
  const box = card(page, "pr-k");
  const text = box.locator('[data-scope="sessions:all"] .disclosure-text');
  await expect(text).toBeHidden();
  await box.getByText("What each scope allows").click();
  await expect(text).toHaveText("This app can read every session on this harness.");
  await expect(box.locator('[data-scope="sessions:all"] .pill')).toHaveText("Elevated");
  expect(errors).toEqual([]);
});

test("before the claim, the page says to claim this Hub first and links to Overview", async ({ page }) => {
  const { errors } = await mount(page, [], { read: () => ({ status: 409,
    json: { detail: "Claim this Hub on Overview before deciding pairing requests.", claimed: false } }) });
  const state = page.locator(".card.empty");
  await expect(state.getByRole("heading", { name: "Claim this Hub first" })).toBeVisible();
  await expect(state.getByRole("link", { name: "Go to Overview" })).toHaveAttribute("href", "#/overview");
  // The state says it once: no refusal sentence beside it.
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a Hub server that does not answer is named, never \"Failed to fetch\", and Retry reads again", async ({ page }) => {
  let down = true;
  const { errors } = await mount(page, [], { read: () => down ? "abort" : null });
  const state = page.locator(".card.empty");
  await expect(state.getByRole("heading", { name: "The Hub server is not answering" })).toBeVisible();
  await expect(page.locator("main")).not.toContainText("Failed to fetch");
  down = false;
  await state.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("heading", { name: "No pairing requests in the last day" })).toBeVisible();
  expect(errors).toEqual([]);
});
