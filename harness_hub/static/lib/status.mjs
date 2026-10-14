// What the shell shows about the daemon (the rail's status chip, the Pairing badge and the banner under each page
// head), worked out from answers the pages already read: Overview's last answer, the Pairing list, the Settings
// revision, the Settings health poll, and any read the Hub refused with 503 because the daemon did not answer. The
// newest evidence about reachability wins, by when its request started. It is held in memory only and makes no calls
// of its own.

// Mirrors MIN_OWNER_API in views.py; the server classifies the reported version as ownership.compatibility.
export const MIN_OWNER_API = "1.24";

let overview = null; // the last overview answer
let lastGood = null; // the last overview answer that reached the daemon
let attention = null; // { approve, confirm }: App pairing requests waiting on the owner
let config = null; // the last Settings revision read: { revision, pending_revision, restart_required }
let silent = null; // whether the latest evidence says the daemon is not answering; null before any
let restarting = false; // whether a restart the Hub started is still under way
let evidenceAt = 0; // when the request behind the latest evidence started; older replies cannot overwrite it
let configAt = 0; // when the request behind `config` (or the health that settled it) started
let reads = 0; // request counter for callers that pass no start order (tests)
const listeners = new Set();

// Whether a 2xx answer to this Hub read could only have come from the daemon. Overview and the health poll report
// reachability in their bodies instead; Apps, Modules and Marketplace answer from the Hub's last good inventory unless
// their state is "ready" (read just now); the Accounts catalog never asks the daemon.
const INVENTORY = new Set(["apps", "modules", "marketplace"]);
function daemonAnswered(path, body) {
  if (INVENTORY.has(path)) return Boolean(body) && body.state === "ready";
  return path === "pairing" || path.startsWith("apps/")
    || (path.startsWith("settings/") && path !== "settings/health")
    || (path.startsWith("manage/") && path !== "manage/catalog");
}

// Let evidence from a request that started at `started` decide reachability, unless newer evidence already has.
function fresh(started) {
  if (started < evidenceAt) return false;
  evidenceAt = started;
  return true;
}

// api.mjs reports every GET it makes, except the session's, with the order in which the request started.
export function noteRead(path, status, data, started = ++reads) {
  reads = Math.max(reads, started);
  const ok = status >= 200 && status < 300;
  const body = ok && data && typeof data === "object" ? data : null;
  if (status === 503) {
    if (fresh(started)) silent = true;
  } else if (path === "overview" && body) {
    if (fresh(started)) {
      overview = body;
      silent = !body.reachable;
      restarting = body.restarting === true;
      if (body.reachable) {
        lastGood = body;
        settle(body.health, started);
      }
      if (body.attention !== undefined) attention = body.attention;
    }
  } else if (path === "settings/health" && body) {
    // Settings polls health around a restart; the Hub answers 200 even when the daemon does not.
    if (typeof body.reachable === "boolean" && fresh(started)) {
      silent = !body.reachable;
      restarting = body.restarting === true;
      if (body.reachable) settle(body.health, started);
    }
  } else if (ok && daemonAnswered(path, body)) {
    if (fresh(started)) silent = false;
  }
  if (body && path === "pairing" && Array.isArray(body.requests)) attention = pairingAttention(body);
  if (body && path === "settings/config" && started >= configAt) {
    config = body;
    configAt = started;
  }
  for (const listener of listeners) listener();
}

// A daemon that reports a confirmed revision at or past the saved (pending) one needs no restart for it, whatever the last
// Settings read said.
function settle(health, started) {
  if (!config || !config.restart_required || !health || health.config_confirmed !== true || started < configAt) return;
  const saved = config.pending_revision;
  if (typeof health.config_revision === "number" && typeof saved === "number" && health.config_revision >= saved) {
    config = { ...config, restart_required: false, revision: health.config_revision, pending_revision: null };
    configAt = started;
  }
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Forget everything (tests).
export function resetStatus() {
  overview = lastGood = attention = config = silent = null;
  restarting = false;
  evidenceAt = configAt = reads = 0;
}

// The same count the server makes for Overview (views.pairing_attention): unexpired App requests that need an
// approval or a confirmation.
function pairingAttention({ requests, now }) {
  const counts = { approve: 0, confirm: 0 };
  for (const row of requests) {
    if (row && row.kind === "app" && row.needs in counts && (row.expires_at || 0) > now) counts[row.needs] += 1;
  }
  return counts;
}

// How many pairing requests need the owner, or null when no page has read them yet.
export function pairingCount() {
  if (!attention) return null;
  return (attention.approve || 0) + (attention.confirm || 0);
}

// The rail's chip: null until Overview has answered or a read found the daemon silent.
export function daemonChip() {
  if (silent) return { tone: "bad", text: "Daemon not answering" };
  if (!overview) return null;
  const version = overview.ownership && overview.ownership.api_version;
  return { tone: "ok", text: version ? `Daemon live · API ${version}` : "Daemon live" };
}

function clockTime(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// The one banner for the slot under the page head, highest priority first: the daemon not answering, an Owner API
// too old, a restart (under way or needed), then an Owner API version that is unknown or too old for tailnet sign-in.
// Returns { tone, title, detail, action } or null; action is { label, href }.
export function daemonBanner() {
  const url = overview && overview.hub ? overview.hub.daemon_url : "";
  if (silent) {
    return {
      tone: "bad",
      title: url ? `The daemon at ${url} is not answering.` : "The daemon is not answering.",
      detail: lastGood && typeof lastGood.fetched_at === "number"
        ? `The Hub last reached it at ${clockTime(lastGood.fetched_at)}. Repeat a change only once it answers.`
        : "Repeat a change only once it answers.",
    };
  }
  const ownership = overview ? overview.ownership : null;
  const compatibility = ownership ? ownership.compatibility : null;
  if (overview && overview.reachable && overview.error) {
    // The daemon answered /health, but the owner API refused or failed (Overview names the HTTP status).
    return { tone: "bad", title: "The daemon answered, but its owner API did not.",
      detail: `${overview.error[0].toUpperCase()}${overview.error.slice(1)}. Overview shows what the Hub last read.` };
  }
  if (compatibility === "too_old") {
    return {
      tone: "bad",
      title: `This daemon's Owner API is ${ownership.api_version}; the Hub needs ${MIN_OWNER_API} or later.`,
      detail: "Update the daemon to agent-harness aa5a904 or later. Some pages will fail until you do.",
    };
  }
  if (restarting) {
    return { tone: "warn", title: "The daemon is restarting.",
      detail: "The Hub checks its health until it answers with confirmed settings." };
  }
  if (config && config.restart_required) {
    const saved = config.pending_revision, running = config.revision;
    return {
      tone: "warn",
      title: "Restart needed to apply saved settings.",
      detail: typeof saved === "number" && typeof running === "number"
        ? `Revision ${saved} is saved; the daemon still runs revision ${running}.` : "",
      action: { label: "Restart in Settings", href: "#/settings" },
    };
  }
  if (compatibility === "no_tailnet_signin") {
    return { tone: "info", title: "Tailnet sign-in needs Owner API 1.26 or later.", detail: "Sign-in links still work." };
  }
  if (compatibility === "unknown") {
    return { tone: "info", title: "This daemon did not report its Owner API version.",
      detail: `The Hub needs ${MIN_OWNER_API} or later; some pages may fail.` };
  }
  return null;
}
