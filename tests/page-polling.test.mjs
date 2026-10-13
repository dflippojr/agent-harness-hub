import assert from "node:assert/strict";
import test from "node:test";
import { renderApps } from "../harness_hub/static/pages/apps.mjs";
import { renderMarketplace } from "../harness_hub/static/pages/marketplace.mjs";
import { renderModules } from "../harness_hub/static/pages/modules.mjs";
import { renderOverview } from "../harness_hub/static/pages/overview.mjs";
import { renderPairing } from "../harness_hub/static/pages/pairing.mjs";
import { renderSettings } from "../harness_hub/static/pages/settings.mjs";
import { deferred, mount, response } from "./page-fake.mjs";

const HOUR = 3600 * 1000;
const claim = (state) => ({ state, message: state, match_code: "123456", expires_at: 900, approve_command: "a", release_command: "r" });
const overview = (state = "claimed", restarting = false) => ({ reachable: false, restarting, claim: claim(state) });
const health = (restarting = false) => ({ restarting, reachable: !restarting, health: { config_revision: 1 } });

// Each page's poll: the requests one poll makes and its interval. A daemon that answers with errors keeps every page
// on its failure path, which still schedules the next poll.
const PAGES = [
  ["Overview", renderOverview, ["/api/overview"], 30000],
  ["Settings", renderSettings, ["/api/settings/health"], 5000],
  ["Modules", renderModules, ["/api/modules", "/api/settings/schema", "/api/settings/config"], 30000],
  ["Apps", renderApps, ["/api/apps", "/api/apps/erasures"], 30000],
  ["Pairing requests", renderPairing, ["/api/pairing"], 5000],
  ["Marketplace", renderMarketplace, ["/api/marketplace"], 5000],
];

function answer(url) {
  if (url === "/api/overview") return Promise.resolve(response(overview()));
  if (url === "/api/settings/health") return Promise.resolve(response(health()));
  return Promise.resolve(response({ detail: "offline" }, 503));
}

const urls = (page, from = 0) => page.calls.slice(from).map((call) => call.url);

for (const [name, render, poll, interval] of PAGES) {
  test(name + " issues no poll while hidden and loads once on becoming visible", async (t) => {
    const page = mount(t, render, answer);
    await page.advance(0);
    await page.visibility("hidden");
    let seen = page.calls.length;
    await page.advance(HOUR);
    assert.deepEqual(urls(page, seen), []);
    assert.equal(page.timers.size, 0);

    await page.visibility("visible");
    assert.deepEqual(urls(page, seen), poll);
    seen = page.calls.length;
    await page.advance(interval - 1);
    assert.deepEqual(urls(page, seen), []);
    await page.advance(1);
    assert.deepEqual(urls(page, seen), poll);
  });

  test(name + " resumes on visible without waiting out a poll that was still pending", async (t) => {
    const page = mount(t, render, answer);
    await page.advance(0);
    await page.visibility("hidden");
    await page.visibility("visible");
    const seen = page.calls.length;
    assert.deepEqual(urls(page, seen - poll.length), poll);
    assert.equal(page.timers.size, 1);
    assert.equal([...page.timers.values()][0].delay, interval);
  });

  test(name + " starts no second load when the tab becomes visible during a load", async (t) => {
    let read = null;
    const page = mount(t, render, (url) => {
      if (read && url === poll[0]) return read.promise.then(() => answer(url));
      return answer(url);
    });
    await page.advance(0);
    read = deferred();
    const seen = page.calls.length;
    const [id, timer] = [...page.timers][0];
    page.timers.delete(id);
    const polling = timer.callback();
    await page.visibility("hidden");
    await page.visibility("visible");
    assert.deepEqual(urls(page, seen), poll);
    read.resolve();
    await polling;
    await page.advance(0);
    assert.deepEqual(urls(page, seen), poll);
    assert.equal(page.timers.size, 1);
  });

  test(name + " teardown leaves no listener, timer or ticker, even while paused", async (t) => {
    const page = mount(t, render, answer);
    await page.advance(0);
    assert.equal(page.listeners.size, 1);
    await page.visibility("hidden");
    await page.advance(HOUR);
    page.stop();
    assert.equal(page.listeners.size, 0);
    assert.equal(page.timers.size, 0);
    assert.equal(page.intervals.size, 0);
    const seen = page.calls.length;
    await page.visibility("visible");
    await page.advance(HOUR);
    assert.equal(page.calls.length, seen);
  });
}

for (const [state, restarting] of [["pending", false], ["persisting", false], ["unclaimed", true]]) {
  test("Overview keeps its 2 s poll while hidden during " + (restarting ? "a restart" : "a " + state + " claim"), async (t) => {
    const page = mount(t, renderOverview, (url) =>
      Promise.resolve(response(url === "/api/overview" ? overview(state, restarting) : health())));
    await page.advance(0);
    await page.visibility("hidden");
    const seen = page.calls.filter((call) => call.url === "/api/overview").length;
    await page.advance(10000);
    assert.equal(page.calls.filter((call) => call.url === "/api/overview").length, seen + 5);
  });
}

test("Overview pauses in a hidden tab once the claim it was waiting on settles", async (t) => {
  let state = "pending";
  const page = mount(t, renderOverview, () => Promise.resolve(response(overview(state))));
  await page.advance(0);
  await page.visibility("hidden");
  await page.advance(2000);
  state = "claimed";
  await page.advance(2000);
  const seen = page.calls.length;
  await page.advance(HOUR);
  assert.equal(page.calls.length, seen);
});

test("Settings keeps its 1 s health poll while hidden during a restart, then pauses", async (t) => {
  let restarting = true;
  const page = mount(t, renderSettings, (url) => url === "/api/settings/health"
    ? Promise.resolve(response(health(restarting))) : Promise.resolve(response({ detail: "offline" }, 503)));
  await page.advance(0);
  await page.visibility("hidden");
  let seen = page.calls.length;
  await page.advance(5000);
  assert.deepEqual(urls(page, seen), Array(5).fill("/api/settings/health"));
  restarting = false;
  await page.advance(1000);
  seen = page.calls.length;
  await page.advance(HOUR);
  assert.deepEqual(urls(page, seen), []);
});
