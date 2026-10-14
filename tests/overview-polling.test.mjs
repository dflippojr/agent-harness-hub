import assert from "node:assert/strict";
import test from "node:test";
import { renderOverview } from "../harness_hub/static/pages/overview.mjs";
import { daemonBanner, daemonChip, resetStatus } from "../harness_hub/static/lib/status.mjs";
import { deferred, mount as mountPage, response, settle } from "./page-fake.mjs";

function mount(t, fetcher) {
  const page = mountPage(t, renderOverview, fetcher);
  const refresh = (el) => el.tag === "button" && el.textContent.includes("Refresh")
    ? [el] : (el.children || []).flatMap(refresh);
  return { ...page, refresh: refresh(page.main)[0].listeners.get("click") };
}

const overview = (state = "unclaimed", restarting = false) => ({
  reachable: false, restarting,
  claim: { state, message: state, match_code: "123456", expires_at: 900, approve_command: "approve", release_command: "release" },
});

for (const failure of [false, true]) {
  test("Overview teardown during restart health " + (failure ? "rejection" : "success") + " cannot rearm polling", async (t) => {
    const health = deferred(), entered = deferred();
    const page = mount(t, (url) => {
      if (url === "/api/overview") return Promise.resolve(response(overview("unclaimed", true)));
      assert.equal(url, "/api/settings/health");
      entered.resolve();
      return health.promise;
    });
    await entered.promise;
    const drawn = page.main.textContent;
    page.stop();
    if (failure) health.reject(new Error("offline"));
    else health.resolve(response({ ok: true }));
    await settle();
    await page.refresh();
    assert.equal(page.timers.size, 0);
    assert.equal(page.listeners.size, 0);
    assert.equal(page.main.textContent, drawn);
    assert.deepEqual(page.calls.map((call) => call.url), ["/api/overview", "/api/settings/health"]);
  });
}

for (const failure of [false, true]) {
  test("Overview teardown during overview " + (failure ? "rejection" : "success") + " ignores the reply", async (t) => {
    const read = deferred();
    const page = mount(t, () => read.promise);
    const drawn = page.main.textContent;
    page.stop();
    if (failure) read.reject(new Error("offline"));
    else read.resolve(response(overview("pending", true)));
    await settle();
    assert.equal(page.main.textContent, drawn);
    assert.equal(page.calls.length, 1);
    assert.equal(page.timers.size, 0);
    assert.equal(page.listeners.size, 0);
  });
}

for (const [state, restarting, delay] of [
  ["pending", false, 2000], ["persisting", false, 2000], ["claimed", false, 30000], ["unclaimed", true, 2000],
]) {
  test("Overview preserves mounted " + state + "/restart=" + restarting + " polling and retires callbacks", async (t) => {
    const page = mount(t, (url) => Promise.resolve(response(url === "/api/overview" ? overview(state, restarting) : { ok: true })));
    await settle();
    assert.equal(page.timers.size, 1);
    const [id, timer] = [...page.timers][0];
    assert.equal(timer.delay, delay);
    page.timers.delete(id);
    await timer.callback();
    assert.equal(page.timers.size, 1);
    const retained = [...page.timers.values()][0].callback;
    const calls = page.calls.length;
    page.stop();
    await retained();
    await page.refresh();
    assert.equal(page.calls.length, calls);
    assert.equal(page.timers.size, 0);
  });
}

test("Overview coalesces overlapping reloads and discards the superseded reply", async (t) => {
  const read = deferred();
  let reads = 0;
  const page = mount(t, () => {
    reads += 1;
    if (reads === 2) return read.promise;
    return Promise.resolve(response(overview(reads === 1 ? "unclaimed" : "claimed")));
  });
  await settle();
  const [id, timer] = [...page.timers][0];
  page.timers.delete(id);
  const polling = timer.callback();
  await page.refresh();
  await page.refresh();
  assert.equal(page.calls.length, 2);
  assert.equal(page.timers.size, 0);
  read.resolve(response(overview("pending", true)));
  await polling;
  await settle();
  assert.deepEqual(page.calls.map((call) => call.url), ["/api/overview", "/api/overview", "/api/overview"]);
  assert.equal(page.timers.size, 1);
  assert.equal([...page.timers.values()][0].delay, 30000);
  assert.match(page.main.textContent, /Claimed/);
  assert.doesNotMatch(page.main.textContent, /Waiting for approval|Daemon restarting/);
});

test("Overview reload after Stop waiting supersedes the in-flight poll", async (t) => {
  const read = deferred(), deletion = deferred();
  let reads = 0;
  const page = mount(t, (url, options) => {
    if (options.method === "DELETE") return deletion.promise;
    reads += 1;
    return reads === 2 ? read.promise : Promise.resolve(response(overview(reads === 1 ? "pending" : "claimed")));
  });
  await settle();
  function find(element) {
    if (element.tag === "button" && element.textContent === "Stop waiting") return element;
    return element.children.map(find).find(Boolean);
  }
  const button = find(page.main);
  assert.ok(button);
  const deleting = button.listeners.get("click")();
  const [id, timer] = [...page.timers][0];
  page.timers.delete(id);
  const polling = timer.callback();
  deletion.resolve(response({ state: "claimed" }));
  await deleting;
  assert.equal(page.calls.filter((call) => call.url === "/api/overview").length, 2);
  read.resolve(response(overview("pending")));
  await polling;
  await settle();
  assert.equal(page.timers.size, 1);
  assert.match(page.main.textContent, /Claimed/);
  assert.equal(page.calls.filter((call) => call.url === "/api/overview").length, 3);
});

const reachableOverview = (ownership) => ({
  reachable: true, restarting: false, error: "", fetched_at: 1,
  hub: { version: "0.0.0", daemon_url: "https://daemon.test" },
  claim: { state: "claimed", message: "claimed", key_id: "k-1", claimed_at: 1 },
  daemon: { release: "0.9.0", build_id: "2026.09.17.1", profile: "service", protocols: {} },
  health: { ok: true, config_revision: 1, config_confirmed: true, supervised_restart: true, recovery: null },
  capabilities: { profile: "service", required: {}, modules: {}, hosted_backends: [], config_registry: true },
  ownership,
});
const ownershipFor = (api_version, compatibility) => ({ api_version, compatibility, daemon_has_hub: true, this_hub: true, record: null });
// The Owner API advice moved from Overview's Daemon card to the shell's banner slot (Hub design 2, #69). The wording
// of each banner and its priority are tested in shell-status.test.mjs; these two check that Overview's read reaches it.
const OWNER_API_BANNERS = [
  ["unknown", "", "info", "This daemon did not report its Owner API version."],
];

for (const [compatibility, version, tone, title] of OWNER_API_BANNERS) {
  test("Overview's read through api() gives the shell's status the " + compatibility + " Owner API banner", async (t) => {
    resetStatus();
    const page = mount(t, (url) => Promise.resolve(response(url === "/api/overview"
      ? reachableOverview(ownershipFor(version, compatibility)) : { ok: true })));
    await settle();
    const banner = daemonBanner();
    assert.equal(banner.tone, tone);
    assert.equal(banner.title, title);
    assert.doesNotMatch(page.main.textContent, /older than the Hub|Tailnet sign-in needs|did not report its Owner API/);
  });
}

test("Overview shows no Owner API banner for a supported daemon", async (t) => {
  resetStatus();
  const page = mount(t, (url) => Promise.resolve(response(url === "/api/overview"
    ? reachableOverview(ownershipFor("1.27", "supported")) : { ok: true })));
  await settle();
  assert.match(page.main.textContent, /Owner API/);
  assert.equal(daemonBanner(), null);
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
});
