import assert from "node:assert/strict";
import test from "node:test";
import { renderOverview } from "../harness_hub/static/pages/overview.mjs";
import { mount, response, settle } from "./page-fake.mjs";

const overview = (attention, extra = {}) => ({
  reachable: true, restarting: false, error: "", attention, fetched_at: 1,
  hub: { version: "0.0.0", daemon_url: "http://127.0.0.1:8085" },
  daemon: {}, health: { ok: true }, capabilities: {}, ownership: null,
  claim: { state: "claimed", message: "claimed", key_id: "k-1", claimed_at: 1 },
  ...extra,
});

function* walk(el) {
  yield el;
  for (const child of el.children || []) yield* walk(child);
}

const labelled = (main, id) => [...walk(main)].find((el) => el.attrs && el.attrs.get("aria-labelledby") === id);

async function draw(t, data) {
  const page = mount(t, renderOverview, () => Promise.resolve(response(data)));
  await settle();
  return page.main;
}

for (const [attention, title, counts] of [
  [{ approve: 1, confirm: 1 }, "2 pairing requests need you", "1 to approve · 1 to confirm."],
  [{ approve: 1, confirm: 0 }, "1 pairing request needs you", "1 to approve."],
  [{ approve: 0, confirm: 3 }, "3 pairing requests need you", "3 to confirm."],
]) {
  test(`Overview's Needs you card says "${title}" with a link to Pairing requests`, async (t) => {
    const shown = labelled(await draw(t, overview(attention)), "attention-title");
    assert.ok(shown, "the Needs you card is drawn");
    assert.equal(shown.attrs.get("role"), "status");
    assert.ok(shown.textContent.startsWith(title), shown.textContent);
    assert.ok(shown.textContent.includes(counts), shown.textContent);
    const link = [...walk(shown)].find((el) => el.tag === "a");
    assert.equal(link.attrs.get("href"), "#/pairing");
    assert.equal(link.textContent, "Review requests");
  });
}

for (const attention of [null, { approve: 0, confirm: 0 }, undefined]) {
  test(`Overview draws no Needs you card for attention ${JSON.stringify(attention)}`, async (t) => {
    assert.equal(labelled(await draw(t, overview(attention)), "attention-title"), undefined);
  });
}

const healthy = overview(null, {
  daemon: { release: "0.9.0", build_id: "2026.09.17.1", profile: "full", protocols: { app: { min: 1, max: 2 } } },
  health: { ok: true, config_revision: 3, config_confirmed: true, supervised_restart: false, recovery: null },
  capabilities: { required: { jobs: true, run_store: true }, modules: { hub: true, web: false }, hosted_backends: [] },
  ownership: { api_version: "1.27", compatibility: "supported", daemon_has_hub: true, this_hub: true,
    record: { name: "UI test Hub", key_id: "k-1" } },
});

test("Overview's tiles show the claim, the daemon and its settings", async (t) => {
  const main = await draw(t, healthy);
  assert.equal(labelled(main, "claim-title"), undefined, "a claimed Hub has no claim card");
  const hub = labelled(main, "tile-this-hub");
  assert.match(hub.textContent, /^This HubClaimedUI test HubKey k-1 · since /);
  assert.equal(labelled(main, "tile-daemon").textContent, "DaemonHealthy0.9.0Owner API 1.27 · supported · profile full");
  assert.equal(labelled(main, "tile-settings").textContent, "SettingsRevision 3ConfirmedRestart is manual here");
  const details = labelled(main, "details-title").textContent;
  for (const fact of ["Daemon addresshttp://127.0.0.1:8085", "Release · build0.9.0 · 2026.09.17.1", "Protocolsapp 1–2",
    "Supervised restartNo", "Daemon records this HubYes, as “UI test Hub”", "Hub version0.0.0"]) {
    assert.ok(details.includes(fact), fact);
  }
});

test("Overview's capabilities count modules and fold the core list into one line", async (t) => {
  const caps = labelled(await draw(t, healthy), "caps-title").textContent;
  assert.ok(caps.includes("Modules · 1 of 2 on"), caps);
  assert.ok(caps.includes("hubweb · off"), caps);
  assert.ok(caps.includes("All 2 core capabilities present"), caps);
  assert.ok(caps.includes("None configured."), caps);
});

test("Overview names a missing core capability and opens the list", async (t) => {
  const data = { ...healthy, capabilities: { ...healthy.capabilities, required: { jobs: true, run_store: false } } };
  const caps = labelled(await draw(t, data), "caps-title");
  const core = [...walk(caps)].find((el) => el.tag === "details");
  assert.equal(core.className, "core bad");
  assert.equal(core.attrs.get("open"), "");
  assert.ok(core.textContent.startsWith("1 of 2 core capabilities missing: run store"), core.textContent);
});

test("Overview's daemon tile says Not answering and keeps the last answer's facts", async (t) => {
  let reads = 0;
  const page = mount(t, renderOverview, () => Promise.resolve(response(++reads === 1 ? healthy
    : overview(null, { reachable: false, daemon: null, health: null, capabilities: null }))));
  await settle();
  await page.main.children[0].children[2].listeners.get("click")();
  await settle();
  assert.match(labelled(page.main, "tile-daemon").textContent, /^DaemonNot answering0\.9\.0Owner API 1\.27/);
  assert.ok(labelled(page.main, "tile-settings"), "the last answer's settings stay");
  assert.match(page.main.children[0].children[1].textContent, /^Last answer .* · retrying every 30 s$/);
});

test("Overview's Hub tile warns when the daemon records a different Hub", async (t) => {
  const data = { ...healthy, ownership: { ...healthy.ownership, this_hub: false, record: { name: "Other", key_id: "k-2" } } };
  const main = await draw(t, data);
  assert.ok(labelled(main, "tile-this-hub").textContent.endsWith(
    "The daemon records a different Hub. Release it on the daemon host, then claim again."));
  assert.ok(labelled(main, "details-title").textContent.includes("Daemon records this HubNo"));
});
