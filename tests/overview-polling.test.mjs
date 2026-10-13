import assert from "node:assert/strict";
import test from "node:test";
import { renderOverview } from "../harness_hub/static/pages/overview.mjs";
import { deferred, mount as mountPage, response, settle } from "./page-fake.mjs";

function mount(t, fetcher) {
  const page = mountPage(t, renderOverview, fetcher);
  return { ...page, refresh: page.main.children[0].children[1].listeners.get("click") };
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
const OWNER_API_NOTICES = [
  ["too_old", "1.23", "This daemon's Owner API (1.23) is older than the Hub supports (1.24, agent-harness aa5a904). Update the daemon; some pages will fail until you do.", "notice bad"],
  ["no_tailnet_signin", "1.25", "Tailnet sign-in needs Owner API 1.26 or later; sign-in links still work.", "notice"],
  ["unknown", "", "This daemon did not report its Owner API version.", "notice"],
];

for (const [compatibility, version, notice, className] of OWNER_API_NOTICES) {
  test("Overview words the " + compatibility + " Owner API notice", async (t) => {
    const page = mount(t, (url) => Promise.resolve(response(url === "/api/overview"
      ? reachableOverview(ownershipFor(version, compatibility)) : { ok: true })));
    await settle();
    const shown = (function find(element) {
      if (element.tag === "p" && element.textContent === notice) return element;
      return element.children.map(find).find(Boolean) ?? null;
    })(page.main);
    assert.ok(shown, notice);
    assert.equal(shown.className, className);
  });
}

test("Overview shows no Owner API notice for a supported daemon", async (t) => {
  const page = mount(t, (url) => Promise.resolve(response(url === "/api/overview"
    ? reachableOverview(ownershipFor("1.27", "supported")) : { ok: true })));
  await settle();
  assert.match(page.main.textContent, /Owner API/);
  assert.doesNotMatch(page.main.textContent, /older than the Hub supports|Tailnet sign-in needs|did not report its Owner API/);
});
