// The shell's daemon status (Hub design 2, #69): the rail chip, the Pairing badge and the banner slot, worked out only
// from reads the pages already make.
import assert from "node:assert/strict";
import test from "node:test";

import { api } from "../harness_hub/static/lib/api.mjs";
import {
  daemonBanner, daemonChip, noteRead, noteRestart, pairingCount, resetStatus,
} from "../harness_hub/static/lib/status.mjs";

const answer = (extra = {}) => ({
  reachable: true, restarting: false, error: "", fetched_at: 1, attention: { approve: 1, confirm: 1 },
  hub: { version: "0.0.0", daemon_url: "https://daemon.test" },
  ownership: { api_version: "1.27", compatibility: "supported" },
  ...extra,
});
// The Hub's own 503 when it could not reach the daemon (server._unreachable).
const GONE = { detail: "The daemon did not answer.", reachable: false };

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
  noteRead("apps", 503, GONE);
  assert.deepEqual(daemonBanner(), { tone: "bad", title: "The daemon at https://daemon.test is not answering.",
    detail: daemonBanner().detail, action: { label: "Retry now", retry: true } });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  noteRead("apps", 200, []);
  assert.equal(daemonBanner().tone, "bad");
  noteRead("overview", 200, answer({ restarting: true, ownership: { api_version: "1.25", compatibility: "no_tailnet_signin" } }));
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  noteRead("overview", 200, answer({ ownership: { api_version: "1.25", compatibility: "no_tailnet_signin" },
    health: { config_revision: 1, config_confirmed: true } }));
  // That restart ended; a Settings read since still has revision 2 waiting (say, the daemon has no supervisor).
  noteRead("settings/config", 200, { revision: 1, pending_revision: 2, restart_required: true });
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
  noteRead("pairing", 503, GONE);
  assert.equal(daemonBanner().title, "The daemon is not answering.");
  noteRead("pairing", 200, { requests: [], now: 1 });
  assert.equal(daemonBanner(), null);
  noteRead("modules", 503, GONE);
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

// The daemon's /health reports the saved revision as soon as it is saved (the active envelope moves to it; only the
// restart-applied keys wait in the pending one), so health at the saved revision is no proof that a restart applied it.
const RESTARTING = { reachable: false, answered: true, status: 503, restarting: true, health: null };
const confirmedAt = (revision) => ({ reachable: true, answered: true, restarting: false,
  health: { ok: true, config_revision: revision, config_confirmed: true } });

test("health at the saved revision keeps restart needed; this page's restart to that revision ending clears it", () => {
  resetStatus();
  noteRead("settings/config", 200, { revision: 2, pending_revision: 2, restart_required: true }, 1);
  noteRead("overview", 200, answer({ health: { config_revision: 2, config_confirmed: true } }), 2);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
  noteRestart(3, 2);
  noteRead("settings/health", 200, RESTARTING, 4);
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  noteRead("settings/health", 200, confirmedAt(2), 5);
  assert.equal(daemonBanner(), null);
  // A later save needs a restart of its own, whatever revision health reports.
  noteRead("settings/config", 200, { revision: 3, pending_revision: 3, restart_required: true }, 6);
  noteRead("settings/health", 200, confirmedAt(3), 7);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
});

test("any page's read that only the daemon could answer clears an older 503 from another page", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  noteRead("apps/erasures", 503, GONE);
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  noteRead("pairing", 200, { requests: [], now: 1 });
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  // A cached inventory or the Hub's own Accounts catalog proves nothing about the daemon.
  noteRead("apps/erasures", 503, GONE);
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
  noteRead("pairing", 503, GONE, 4);
  assert.equal(daemonBanner().tone, "bad");
});

test("an inventory read reports whether the daemon answered, whatever its state", () => {
  resetStatus();
  noteRead("pairing", 503, GONE);
  noteRead("marketplace", 200, { state: "ready", stale: false, reachable: true, entries: [] });
  assert.equal(daemonBanner(), null);
  // The daemon stops while the owner stays on Marketplace: the Hub still answers 200 from its last good inventory.
  noteRead("overview", 200, answer());
  noteRead("marketplace", 200, { state: "error", error: "The daemon did not answer.", stale: true, reachable: false });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  // Any answer from the daemon, even one the Hub withholds or a module switched off, proves it is answering.
  for (const path of ["modules", "apps"]) {
    noteRead("marketplace", 200, { state: "error", stale: true, reachable: false });
    noteRead(path, 200, { state: path === "apps" ? "error" : "disabled", stale: true, reachable: true });
    assert.equal(daemonChip().tone, "ok");
  }
});

test("a Spend read reports whether the daemon answered, like the inventory pages", () => {
  resetStatus();
  noteRead("pairing", 503, GONE);
  noteRead("spend", 200, { error: "", stale: false, reachable: true, apps: [], backends: [] });
  assert.equal(daemonBanner(), null);
  noteRead("overview", 200, answer());
  noteRead("spend", 200, { error: "The daemon did not answer.", stale: true, reachable: false, apps: [], backends: [] });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  noteRead("spend", 200, { error: "", stale: false, reachable: true, apps: [], backends: [] });
  assert.equal(daemonChip().tone, "ok");
});

test("a served Spend read clears an owner API failure; an errored one does not", () => {
  resetStatus();
  noteRead("overview", 200, answer({ error: "owner API 500" }));
  assert.equal(daemonBanner().tone, "bad");
  noteRead("spend", 200, { error: "The daemon answered HTTP 500", stale: true, reachable: true, apps: [] });
  assert.equal(daemonBanner().tone, "bad");
  noteRead("spend", 200, { error: "", stale: false, reachable: true, apps: [], backends: [] });
  assert.equal(daemonBanner(), null);
});

test("a Settings read made during this page's restart is settled when that restart ends", () => {
  resetStatus();
  noteRestart(1, 4);
  noteRead("settings/health", 200, RESTARTING, 2);
  // The daemon keeps revision 4 pending until the restarted process confirms it.
  noteRead("settings/config", 200, { revision: 4, pending_revision: 4, restart_required: true }, 3);
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  noteRead("settings/health", 200, confirmedAt(4), 4);
  assert.equal(daemonBanner(), null);
});

test("a restart that recovered no longer covers a revision saved after it, even one numbered the same", () => {
  resetStatus();
  noteRead("settings/health", 200, confirmedAt(3), 1);
  noteRestart(2, 4);
  // Revision 4 fails to boot: the daemon restores revision 3, and the next save is numbered 4 again.
  noteRead("settings/config", 200, { revision: 4, pending_revision: 4, restart_required: true, recovery: LKG }, 3);
  noteRead("settings/health", 200, { ...confirmedAt(3), health: { ...confirmedAt(3).health, recovery: LKG } }, 4);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
});

test("health requested before this page's restart cannot drop its target, whatever recovery it reports", () => {
  resetStatus();
  // An earlier recovery is already known from Settings; a health read that started before the restart answers late.
  noteRead("settings/config", 200, { revision: 4, pending_revision: 4, restart_required: true, recovery: LKG }, 1);
  noteRestart(3, 4);
  noteRead("settings/health", 200, { ...confirmedAt(3), health: { ...confirmedAt(3).health, recovery: LKG } }, 2);
  noteRead("settings/health", 200, { ...confirmedAt(4), health: { ...confirmedAt(4).health, recovery: LKG } }, 4);
  assert.equal(daemonBanner(), null);
});

test("a Settings config reply older than the health that settled the restart cannot restore it", () => {
  resetStatus();
  noteRead("settings/config", 200, { revision: 2, pending_revision: 2, restart_required: true }, 1);
  noteRestart(2, 2);
  noteRead("settings/health", 200, confirmedAt(2), 4);
  // Read during the restart: the daemon keeps the pending revision until the restarted process confirms it.
  noteRead("settings/config", 200, { revision: 2, pending_revision: 2, restart_required: true }, 3);
  assert.equal(daemonBanner(), null);
  noteRead("settings/config", 200, { revision: 3, pending_revision: 3, restart_required: true }, 5);
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
  noteRead("pairing", 503, GONE, 6);
  assert.equal(daemonBanner().title, "The daemon at https://daemon.test is not answering.");
  assert.match(daemonBanner().detail, new RegExp(new Date(200000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
});

test("a restart's end settles a Settings reply that started before the restart but came later", () => {
  resetStatus();
  noteRestart(2, 2);
  noteRead("settings/health", 200, confirmedAt(2), 3);
  noteRead("settings/config", 200, { revision: 2, pending_revision: 2, restart_required: true }, 1);
  assert.equal(daemonBanner(), null);
  // A newer Settings read still wins over that health answer.
  noteRead("settings/config", 200, { revision: 3, pending_revision: 3, restart_required: true }, 3);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
});

test("an inventory error from the daemon leaves an owner API failure standing; a served inventory clears it", () => {
  resetStatus();
  noteRead("overview", 200, answer({ error: "the owner API answered HTTP 500", ownership: null }));
  noteRead("modules", 200, { state: "error", error: "The daemon answered HTTP 500", stale: true, reachable: true });
  assert.equal(daemonBanner().title, "The daemon answered, but its owner API did not.");
  noteRead("modules", 200, { state: "ready", stale: false, reachable: true });
  assert.equal(daemonBanner(), null);
});

test("a newer health poll cannot hide an older Overview's owner API failure; a newer owner API read can", () => {
  resetStatus();
  noteRead("overview", 200, answer(), 1);
  // Overview's request started at 2; the Settings health poll started at 3 and answered first.
  noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } }, 3);
  noteRead("overview", 200, answer({ error: "the owner API answered HTTP 500", ownership: null }), 2);
  assert.equal(daemonBanner().title, "The daemon answered, but its owner API did not.");
  noteRead("settings/config", 200, { revision: 1, pending_revision: null, restart_required: false }, 4);
  assert.equal(daemonBanner(), null);
  noteRead("overview", 200, answer({ error: "the owner API answered HTTP 500", ownership: null }), 2);
  assert.equal(daemonBanner(), null);
});

test("an error the daemon answered with, 503 included, is not silence; only the Hub's own unreachable 503 is", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  // The daemon's own 503, forwarded (server._forwarded): it answered.
  noteRead("pairing", 503, { detail: "pairing store is busy", error: { code: "unavailable" }, reachable: true });
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  assert.equal(daemonBanner(), null);
  noteRead("pairing", 503, GONE);
  assert.equal(daemonChip().text, "Daemon not answering");
  // A forwarded error proves the daemon answers again, on any page, without saying its owner API works.
  noteRead("settings/config", 500, { detail: "The daemon answered HTTP 500", error: { code: "" }, reachable: true });
  assert.equal(daemonChip().text, "Daemon live · API 1.27");
  assert.equal(daemonBanner(), null);
  // A Hub refusal that never asked the daemon (signed out, unclaimed, a bad request) says nothing either way.
  noteRead("pairing", 503, GONE);
  noteRead("settings/config", 409, { detail: "Claim this Hub on Overview before administering Settings." });
  noteRead("apps/k1/limits", 401, { detail: "sign in" });
  noteRead("settings/backends", 503, { detail: "unmarked" });
  assert.equal(daemonChip().text, "Daemon not answering");
  // Newest evidence still wins: an older forwarded error cannot clear a newer unreachable 503.
  resetStatus();
  noteRead("pairing", 503, GONE, 5);
  noteRead("pairing", 503, { detail: "busy", reachable: true }, 4);
  assert.equal(daemonChip().text, "Daemon not answering");
  // Nor does a forwarded error clear Overview's owner API failure.
  resetStatus();
  noteRead("overview", 200, answer({ error: "the owner API answered HTTP 500", ownership: null }));
  noteRead("pairing", 500, { detail: "The daemon answered HTTP 500", reachable: true });
  assert.equal(daemonBanner().title, "The daemon answered, but its owner API did not.");
});

const LKG = { recovery: "lkg_restore", reason: "boot failed", warning: "", at: 50 };

test("a restart that recovery resolved at a lower revision clears restart needed, in either order", () => {
  resetStatus();
  noteRead("settings/config", 200, { revision: 3, pending_revision: 4, restart_required: true, recovery: null }, 1);
  noteRead("overview", 200, answer({ restarting: true }), 2);
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  // Revision 4 failed to boot; the daemon restored its last good revision 3 and threw revision 4 out.
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: true, recovery: LKG } }), 3);
  assert.equal(daemonBanner(), null);
  // An overlay quarantine boots on defaults, revision 0.
  resetStatus();
  noteRead("overview", 200, answer({ health: { config_revision: 0, config_confirmed: true,
    recovery: { recovery: "overlay_quarantined", reason: "bad overlay", warning: "bad overlay", at: 60 } } }), 2);
  noteRead("settings/config", 200, { revision: 3, pending_revision: 4, restart_required: true, recovery: null }, 1);
  assert.equal(daemonBanner(), null);
});

test("a recovery the Settings read already reported does not settle a revision saved after it", () => {
  resetStatus();
  // The daemon recovered at an earlier boot and still reports it; revision 4 was saved since.
  noteRead("settings/config", 200, { revision: 3, pending_revision: 4, restart_required: true, recovery: LKG }, 1);
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: true, recovery: LKG } }), 2);
  assert.deepEqual(daemonBanner(), { tone: "warn", title: "Restart needed to apply saved settings.",
    detail: "Revision 4 is saved; the daemon still runs revision 3.",
    action: { label: "Restart in Settings", href: "#/settings" } });
  // A new recovery (a later boot) settles it; an unconfirmed health answer, or one older than the read, does not.
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: false,
    recovery: { ...LKG, at: 90 } } }), 3);
  noteRead("settings/health", 200, { reachable: true, restarting: false,
    health: { config_revision: 3, config_confirmed: true, recovery: { ...LKG, at: 80 } } }, 0);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
  noteRead("settings/health", 200, { reachable: true, restarting: false,
    health: { config_revision: 3, config_confirmed: true, recovery: { ...LKG, at: 90 } } }, 4);
  assert.equal(daemonBanner(), null);
});

test("a Hub restart that began after the Settings read and has ended settles it, even with no pending revision", () => {
  resetStatus();
  // The daemon's restart keys differ from the running process: a restart is needed with nothing pending.
  noteRead("settings/config", 200, { revision: 3, pending_revision: null, restart_required: true }, 1);
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: true } }), 2);
  assert.deepEqual(daemonBanner(), { tone: "warn", title: "Restart needed to apply saved settings.", detail: "",
    action: { label: "Restart in Settings", href: "#/settings" } });
  // Confirmed health while the restart is still under way settles nothing.
  noteRead("overview", 200, answer({ restarting: true, health: { config_revision: 3, config_confirmed: true } }), 3);
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  // The Overview reply that saw it end arrives before the Settings poll that saw it under way: same outcome.
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: true } }), 5);
  noteRead("settings/health", 200, { reachable: true, restarting: true, health: null }, 4);
  assert.equal(daemonBanner(), null);
  // A restart that ended before the Settings read says nothing about it.
  resetStatus();
  noteRead("overview", 200, answer({ restarting: true }), 1);
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: true } }), 2);
  noteRead("settings/config", 200, { revision: 3, pending_revision: 4, restart_required: true }, 3);
  noteRead("overview", 200, answer({ health: { config_revision: 3, config_confirmed: true } }), 4);
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
});

test("a daemon that answers /health with an error is answering: a restart shows its banner, not an outage", () => {
  resetStatus();
  noteRead("overview", 200, answer());
  // Mid-restart the daemon answers /health with 503: the Hub read no health answer, but the daemon answered.
  noteRead("settings/health", 200, { reachable: false, answered: true, status: 503, restarting: true, health: null });
  assert.deepEqual(daemonChip(), { tone: "warn", text: "Daemon restarting" });
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  noteRead("overview", 200, answer({ reachable: false, answered: true, restarting: true, ownership: null,
    error: "the daemon's /health answered HTTP 503" }));
  assert.deepEqual(daemonChip(), { tone: "warn", text: "Daemon restarting" });
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  // With no restart under way, the error is the daemon's own, still not silence.
  noteRead("settings/health", 200, { reachable: false, answered: true, status: 503, restarting: false, health: null });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon unhealthy" });
  assert.equal(daemonBanner().title, "The daemon answered, but its health check failed.");
  // Only the Hub failing to reach the daemon is not answering.
  noteRead("settings/health", 200, { reachable: false, answered: false, restarting: true, health: null });
  assert.deepEqual(daemonChip(), { tone: "bad", text: "Daemon not answering" });
  noteRead("settings/health", 200, { reachable: true, answered: true, restarting: false, health: { ok: true } });
  assert.deepEqual(daemonChip(), { tone: "ok", text: "Daemon live · API 1.27" });
  assert.equal(daemonBanner(), null);
});


test("ending an older restart does not settle a revision saved during it", () => {
  for (const [reverse, own] of [[false, false], [true, false], [false, true], [true, true]]) {
    resetStatus();
    noteRead("settings/config", 200, { revision: 4, pending_revision: 4, restart_required: true }, 1);
    // This page started the restart to revision 4, or another tab did and this one only saw it under way.
    if (own) noteRestart(2, 4);
    else noteRead("overview", 200, answer({ restarting: true }), 2);
    noteRead("settings/config", 200, { revision: 5, pending_revision: 5, restart_required: true }, 3);
    const active = () => noteRead("settings/health", 200, { reachable: true, restarting: true, health: null }, 4);
    const done = () => noteRead("overview", 200, answer({ health: { config_revision: 4, config_confirmed: true } }), 5);
    if (reverse) { done(); active(); } else { active(); done(); }
    assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
    assert.equal(daemonBanner().detail, "Revision 5 is saved; the daemon still runs revision 4.");
    // Health at revision 5 is only the save; a restart to it applies it.
    noteRead("settings/health", 200, confirmedAt(5), 6);
    assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
    noteRestart(7, 5);
    noteRead("settings/health", 200, confirmedAt(5), 8);
    assert.equal(daemonBanner(), null);
  }
});

test("owner API replies change reachability without erasing a health failure", () => {
  for (const [path, status, data] of [
    ["settings/backends", 200, {}],
    ["pairing", 503, { reachable: true }],
    ["modules", 200, { reachable: true, state: "ready" }],
    ["spend", 200, { reachable: true, error: "" }],
  ]) {
    resetStatus();
    noteRead("overview", 200, answer(), 1);
    noteRead("settings/health", 200, { reachable: false, answered: true, restarting: false, health: null }, 3);
    noteRead(path, status, data, 5);
    assert.equal(daemonChip().text, "Daemon unhealthy", path);
    assert.equal(daemonBanner().title, "The daemon answered, but its health check failed.");
    // An older healthy read is still older health evidence, despite the newer owner API reply.
    noteRead("overview", 200, answer(), 2);
    assert.equal(daemonChip().text, "Daemon unhealthy");
    noteRead("pairing", 503, GONE, 6);
    assert.equal(daemonChip().text, "Daemon not answering");
    // Health has its own order: this delayed poll predates the owner API read, but follows the health failure.
    noteRead("settings/health", 200, { reachable: true, restarting: false, health: { ok: true } }, 4);
    assert.equal(daemonChip().text, "Daemon not answering");
    noteRead(path, status, data, 7);
    assert.equal(daemonChip().tone, "ok");
  }
});

test("an accepted restart settles a warning even when navigation skips every active health poll", async (t) => {
  resetStatus();
  const originalFetch = globalThis.fetch;
  const originalStorage = globalThis.sessionStorage;
  t.after(() => { globalThis.fetch = originalFetch; globalThis.sessionStorage = originalStorage; });
  globalThis.sessionStorage = { getItem: () => null };
  let accepted = false;
  globalThis.fetch = async (url) => new Response(JSON.stringify(
    url === "/api/settings/config" ? { revision: 3, pending_revision: null, restart_required: true } :
    url === "/api/settings/restart" ? { accepted, target_revision: 3 } :
    answer({ health: { config_revision: 3, config_confirmed: true } })), { status: 200 });
  await api("settings/config");
  await api("settings/restart", { method: "POST", body: { revision: 3, confirm: true } });
  await api("overview");
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
  accepted = true;
  await api("settings/restart", { method: "POST", body: { revision: 3, confirm: true } });
  // The owner moves to Apps before a poll fires, then returns to Overview after completion.
  await api("overview");
  assert.equal(daemonBanner(), null);
});


test("health requested before restart acceptance cannot settle that accepted restart", async (t) => {
  resetStatus();
  const originalFetch = globalThis.fetch;
  const originalStorage = globalThis.sessionStorage;
  t.after(() => { globalThis.fetch = originalFetch; globalThis.sessionStorage = originalStorage; });
  globalThis.sessionStorage = { getItem: () => null };
  let accept;
  globalThis.fetch = async (url) => {
    if (url === "/api/settings/restart") return new Promise((resolve) => {
      accept = () => resolve(new Response(JSON.stringify({ accepted: true, target_revision: 3 }), { status: 202 }));
    });
    return new Response(JSON.stringify(url === "/api/settings/config"
      ? { revision: 3, pending_revision: null, restart_required: true }
      : answer({ health: { config_revision: 3, config_confirmed: true } })), { status: 200 });
  };
  await api("settings/config");
  const pending = api("settings/restart", { method: "POST", body: { revision: 3, confirm: true } });
  await api("overview");
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
  accept();
  await pending;
  assert.equal(daemonBanner().title, "The daemon is restarting.");
  await api("overview");
  assert.equal(daemonBanner(), null);
  // A subsequent Settings read that still asks for a restart is newer evidence.
  await api("settings/config");
  await api("overview");
  assert.equal(daemonBanner().title, "Restart needed to apply saved settings.");
});
