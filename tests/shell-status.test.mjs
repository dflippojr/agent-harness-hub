// The shell's daemon status (Hub design 2, #69): the rail chip, the Pairing badge and the banner slot, worked out only
// from reads the pages already make.
import assert from "node:assert/strict";
import test from "node:test";

import { daemonBanner, daemonChip, noteRead, pairingCount, resetStatus } from "../harness_hub/static/lib/status.mjs";

const answer = (extra = {}) => ({
  reachable: true, restarting: false, error: "", fetched_at: 1, attention: { approve: 1, confirm: 1 },
  hub: { version: "0.0.0", daemon_url: "https://daemon.test" },
  ownership: { api_version: "1.27", compatibility: "supported" },
  ...extra,
});

test("nothing is shown before any page has read the daemon", () => {
  resetStatus();
  assert.equal(daemonChip(), null);
  assert.equal(daemonBanner(), null);
  assert.equal(pairingCount(), null);
});

test("a healthy Overview answer gives a live chip, the badge count and no banner", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  assert.equal(pairingCount(), 2);
  assert.equal(daemonBanner(), null);
});

test("the banner keeps the priority order: not answering, too old, restarting, restart needed, version advice", () => {
  resetStatus();
  noteRead("settings/config", 200, { revision: 1, pending_revision: 2, restart_required: true });
  noteRead("overview", 200, answer({ restarting: true, ownership: { api_version: "1.20", compatibility: "too_old" } }));
  assert.equal(daemonBanner().title, "This daemon's Owner API is 1.20; the Hub needs 1.24 or later.");
  noteRead("apps", 503, { detail: "The daemon did not answer." });
  assert.deepEqual(daemonBanner(), { tone: "bad", title: "The daemon at https://daemon.test is not answering.",
    detail: daemonBanner().detail });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  noteRead("apps", 200, []);
  assert.equal(daemonBanner().tone, "bad");
  noteRead("overview", 200, answer({ restarting: true, ownership: { api_version: "1.25", compatibility: "no_tailnet_signin" } }));
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  noteRead("overview", 200, answer({ ownership: { api_version: "1.25", compatibility: "no_tailnet_signin" },
    health: { config_revision: 1, config_confirmed: true } }));
  assert.deepEqual(daemonBanner(), { tone: "warn", title: "Restart needed to apply saved settings.",
    detail: "Revision 2 is saved; the daemon still runs revision 1.",
    action: { label: "Restart in Settings", href: "#/settings" } });
  noteRead("settings/config", 200, { revision: 2, pending_revision: null, restart_required: false });
  assert.equal(daemonBanner().title, "Tailnet sign-in needs Owner API 1.26 or later.");
});

test("an unreachable Overview answer is not answering until one reaches the daemon again", () => {
  resetStatus();
  noteRead("overview", 200, answer({ fetched_at: 100 }));
  noteRead("overview", 200, answer({ reachable: false, error: "the daemon did not answer", ownership: null }));
  const shown = daemonBanner();
  assert.equal(shown.tone, "bad");
  assert.match(shown.detail, /^The Hub last reached it at /);
  noteRead("overview", 200, answer());
  assert.equal(daemonBanner(), null);
});

test("a page's 503 read is cleared by the same read answering, or by Overview reaching the daemon", () => {
  resetStatus();
  noteRead("pairing", 503, { detail: "The daemon did not answer." });
  assert.equal(daemonBanner().title, "The daemon is not answering.");
  noteRead("pairing", 200, { requests: [], now: 1 });
  assert.equal(daemonBanner(), null);
  noteRead("modules", 503, null);
  noteRead("overview", 200, answer());
  assert.equal(daemonBanner(), null);
  // Other refusals are the page's own business, not the daemon's silence.
  noteRead("apps", 409, { detail: "Claim this Hub first." });
  assert.equal(daemonBanner(), null);
});

test("the Pairing list keeps the badge current with the server's count rule", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  noteRead("pairing", 200, { now: 50, requests: [
    { kind: "app", needs: "approve", expires_at: 60 },
    { kind: "app", needs: "confirm", expires_at: 60 },
    { kind: "app", needs: "approve", expires_at: 40 },
    { kind: "hub", needs: "approve", expires_at: 60 },
    { kind: "app", needs: "claim", expires_at: 60 },
  ] });
  assert.equal(pairingCount(), 2);
  noteRead("pairing", 200, { now: 50, requests: [] });
  assert.equal(pairingCount(), 0);
});

test("a Settings health poll that cannot reach the daemon turns the chip and banner to not answering", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  noteRead("settings/health", 200, { reachable: false, restarting: true, health: null });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  assert.equal(daemonBanner().tone, "bad");
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } });
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  assert.equal(daemonBanner(), null);
});

test("an owner API failure behind a healthy /health is a bad banner, not silence", () => {
  resetStatus();
  noteRead("overview", 200, answer({ error: "the owner API answered HTTP 500", ownership: null }));
  assert.deepEqual(daemonBanner(), { tone: "bad", title: "The daemon answered, but its owner API did not.",
    detail: "The owner API answered HTTP 500. Overview shows what the Hub last read." });
});

test("a reachable Settings health poll supersedes Overview's outage and restart", () => {
  resetStatus();
  noteRead("overview", 200, answer({ reachable: false, restarting: true, error: "the daemon did not answer", ownership: null }));
  assert.equal(daemonBanner().tone, "bad");
  noteRead("settings/health", 200, { reachable: true, restarting: true, health: { ok: true } });
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } });
  assert.equal(daemonBanner(), null);
});

test("restart needed clears once the daemon confirms the saved revision", () => {
  resetStatus();
  noteRead("settings/config", 200, { revision: 1, pending_revision: 2, restart_required: true });
  noteRead("overview", 200, answer({ health: { config_revision: 1, config_confirmed: true } }));
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
  noteRead("overview", 200, answer({ health: { config_revision: 2, config_confirmed: false } }));
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
  noteRead("overview", 200, answer({ health: { config_revision: 2, config_confirmed: true } }));
  assert.equal(daemonBanner(), null);
  noteRead("settings/config", 200, { revision: 2, pending_revision: 3, restart_required: true });
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { config_revision: 3, config_confirmed: true } });
  assert.equal(daemonBanner(), null);
});

test("any page's read that only the daemon could answer clears an older 503 from another page", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  noteRead("apps/erasures", 503, { detail: "The daemon did not answer." });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  noteRead("pairing", 200, { requests: [], now: 1 });
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  // A cached inventory or the Hub's own Accounts catalog proves nothing about the daemon.
  noteRead("apps/erasures", 503, null);
  for (const path of ["modules", "apps", "marketplace", "manage/catalog"]) noteRead(path, 200, { state: "error", stale: true });
  assert.equal(daemonChip().tone, "bad");
  noteRead("manage/skills", 200, []);
  assert.equal(daemonChip().tone, "ok");
});

test("a reply to an older request cannot overwrite newer evidence", () => {
  resetStatus();
  noteRead("overview", 200, answer(), 1);
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } }, 3);
  // Overview's request started at 2, before the health poll, but answered after it.
  noteRead("overview", 200, answer({ reachable: false, error: "the daemon did not answer", ownership: null }), 2);
  assert.equal(daemonBanner(), null);
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  noteRead("pairing", 503, null, 4);
  assert.equal(daemonBanner().tone, "bad");
});

test("an inventory read the daemon just answered (state ready) proves it is answering", () => {
  resetStatus();
  noteRead("pairing", 503, null);
  noteRead("marketplace", 200, { state: "ready", stale: false, entries: [] });
  assert.equal(daemonBanner(), null);
});

test("a Settings config reply older than the health that settled the restart cannot restore it", () => {
  resetStatus();
  noteRead("settings/config", 200, { revision: 1, pending_revision: 2, restart_required: true }, 1);
  noteRead("settings/health", 200, { reachable: true, restarting: false,
    health: { config_revision: 2, config_confirmed: true } }, 3);
  noteRead("settings/config", 200, { revision: 1, pending_revision: 2, restart_required: true }, 2);
  assert.equal(daemonBanner(), null);
  noteRead("settings/config", 200, { revision: 2, pending_revision: 3, restart_required: true }, 4);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
});

test("a Pairing reply older than Overview's count cannot overwrite it, nor the other way round", () => {
  resetStatus();
  const one = { now: 50, requests: [{ kind: "app", needs: "approve", expires_at: 60 }] };
  // The Pairing poll started at 1, the owner left for Overview (2), and Pairing answered last.
  noteRead("overview", 200, answer({ attention: { approve: 2, confirm: 1 } }), 2);
  noteRead("pairing", 200, one, 1);
  assert.equal(pairingCount(), 3);
  noteRead("pairing", 200, one, 3);
  assert.equal(pairingCount(), 1);
  noteRead("overview", 200, answer({ attention: { approve: 0, confirm: 0 } }), 2);
  assert.equal(pairingCount(), 1);
});

test("the restart banner names the running revision only from the daemon's health, never the saved revision", () => {
  resetStatus();
  // Saving a restart-required change: the registry's revision and pending revision are both the saved one.
  noteRead("settings/config", 200, { revision: 4, pending_revision: 4, restart_required: true }, 1);
  assert.equal(daemonBanner().detail, "Revision 4 is saved; restart the daemon to apply it.");
  noteRead("settings/health", 200, { reachable: true, restarting: false,
    health: { config_revision: 3, config_confirmed: true } }, 2);
  assert.equal(daemonBanner().detail, "Revision 4 is saved; the daemon still runs revision 3.");
  // An older health answer cannot replace the running revision a newer one reported.
  noteRead("overview", 200, answer({ health: { config_revision: 2, config_confirmed: true } }), 1);
  assert.equal(daemonBanner().detail, "Revision 4 is saved; the daemon still runs revision 3.");
  // A running revision at or past the saved one, but unconfirmed, is not claimed as older.
  noteRead("settings/health", 200, { reachable: true, restarting: false,
    health: { config_revision: 4, config_confirmed: false } }, 3);
  assert.equal(daemonBanner().detail, "Revision 4 is saved; restart the daemon to apply it.");
});

test("an owner API failure clears once a newer owner API read answers, not on an older one or a bare /health", () => {
  resetStatus();
  noteRead("overview", 200, answer({ error: "the owner API answered HTTP 500", ownership: null }), 2);
  noteRead("pairing", 200, { requests: [], now: 1 }, 1);
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } }, 3);
  assert.equal(daemonBanner().title, "The daemon answered, but its owner API did not.");
  noteRead("pairing", 200, { requests: [], now: 1 }, 4);
  assert.equal(daemonBanner(), null);
});

test("an older reply cannot restore a restart, an Overview answer or the last time the Hub reached the daemon", () => {
  resetStatus();
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } }, 3);
  noteRead("overview", 200, answer({ restarting: true }), 2);
  assert.equal(daemonBanner(), null);
  noteRead("overview", 200, answer({ fetched_at: 200 }), 5);
  noteRead("overview", 200, answer({ fetched_at: 100, hub: { version: "0", daemon_url: "https://old.test" } }), 4);
  noteRead("pairing", 503, null, 6);
  assert.equal(daemonBanner().title, "The daemon at https://daemon.test is not answering.");
  assert.match(daemonBanner().detail, new RegExp(new Date(200000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
});
