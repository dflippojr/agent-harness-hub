// What the shell shows about the daemon (the rail's status chip, the Pairing badge and the banner under each page
// head), worked out from answers the pages already read: Overview's last answer, the Pairing list, the Settings
// revision, the Settings health poll, and any read the Hub refused with 503 because the daemon did not answer. For each
// piece of state the newest evidence wins, by when its request started. It is held in memory only and makes no calls
// of its own.

// Mirrors MIN_OWNER_API in views.py; the server classifies the reported version as ownership.compatibility.
export const MIN_OWNER_API = "1.24";

let overview = null; // the last overview answer
let lastGood = null; // the last overview answer that reached the daemon
let attention = null; // { approve, confirm }: App pairing requests waiting on the owner
let config = null; // the last Settings revision read (saved, not running): { revision, pending_revision, restart_required }
let running = null; // the settings revision the daemon reported in its last health answer
let confirmed = null; // { revision, started }: the newest health answer with a confirmed settings revision
let silent = null; // whether the latest evidence says the daemon is not answering; null before any
let ownerError = ""; // Overview's owner API failure behind a healthy /health, until an owner API read answers
let restarting = false; // whether a restart the Hub started is still under way
let at = {}; // per piece of state, when the request behind its current value started
let reads = 0; // request counter for callers that pass no start order (tests)
const listeners = new Set();

// Whether a 2xx answer to this Hub read could only have come from the daemon's owner API. Overview and the health poll
// report reachability in their bodies instead, and so do Apps, Modules and Marketplace, which answer 200 from the Hub's
// last good inventory either way; the Accounts catalog never asks the daemon.
const INVENTORY = new Set(["apps", "modules", "marketplace"]);
function daemonAnswered(path) {
  return path === "pairing" || path.startsWith("apps/")
    || (path.startsWith("settings/") && path !== "settings/health")
    || (path.startsWith("manage/") && path !== "manage/catalog");
}

// Let a reply to a request that started at `started` set this piece of state, unless a newer request already has. A
// slower reply to an older request never overwrites what a newer one said.
function newer(key, started) {
  if (started < (at[key] || 0)) return false;
  at[key] = started;
  return true;
}

// api.mjs reports every GET it makes, except the session's, with the order in which the request started.
export function noteRead(path, status, data, started = ++reads) {
  reads = Math.max(reads, started);
  const ok = status >= 200 && status < 300;
  const body = ok && data && typeof data === "object" ? data : null;
  if (status === 503) {
    if (newer("reach", started)) silent = true;
  } else if (path === "overview" && body) {
    if (newer("overview", started)) overview = body;
    if (body.reachable && newer("lastGood", started)) lastGood = body;
    if (newer("reach", started)) silent = !body.reachable;
    // Owner API evidence has its own order: a health poll says nothing about the owner API, so it cannot hide this.
    if (newer("owner", started)) ownerError = body.reachable ? body.error || "" : "";
    if (newer("restart", started)) restarting = body.restarting === true;
    if (body.reachable) health(body.health, started);
    if (body.attention !== undefined && newer("attention", started)) attention = body.attention;
  } else if (path === "settings/health" && body) {
    // Settings polls health around a restart; the Hub answers 200 even when the daemon does not. A healthy /health
    // says nothing about the owner API, so it leaves an owner API failure standing.
    if (typeof body.reachable === "boolean") {
      if (newer("reach", started)) silent = !body.reachable;
      if (newer("restart", started)) restarting = body.restarting === true;
      if (body.reachable) health(body.health, started);
    }
  } else if (INVENTORY.has(path) && body) {
    if (typeof body.reachable === "boolean" && newer("reach", started)) silent = !body.reachable;
    // Only an inventory the daemon served ("ready") shows its owner API answering; an HTTP error from it does not.
    if (body.state === "ready" && newer("owner", started)) ownerError = "";
  } else if (ok && daemonAnswered(path)) {
    if (newer("reach", started)) silent = false;
    if (newer("owner", started)) ownerError = "";
  }
  if (body && path === "pairing" && Array.isArray(body.requests) && newer("attention", started)) {
    attention = pairingAttention(body);
  }
  if (body && path === "settings/config" && newer("config", started)) {
    config = body;
    settle();
  }
  for (const listener of listeners) listener();
}

// A health answer from the daemon: the revision it runs, and the newest confirmed one, which may settle a restart.
function health(reported, started) {
  if (!reported || typeof reported.config_revision !== "number") return;
  if (newer("running", started)) running = reported.config_revision;
  if (reported.config_confirmed === true && newer("confirmed", started)) {
    confirmed = { revision: reported.config_revision, started };
    settle();
  }
}

// A daemon that confirmed a revision at or past the saved (pending) one needs no restart for it, whatever an older
// Settings read said, in whichever order the two answers arrive.
function settle() {
  if (!config || !config.restart_required || !confirmed || confirmed.started < (at.config || 0)) return;
  const saved = config.pending_revision;
  if (typeof saved === "number" && confirmed.revision >= saved) {
    config = { ...config, restart_required: false, pending_revision: null };
    at.config = confirmed.started;
  }
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Forget everything (tests).
export function resetStatus() {
  overview = lastGood = attention = config = running = confirmed = silent = null;
  ownerError = "";
  restarting = false;
  at = {};
  reads = 0;
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
  // Ownership as of the newest Overview answer that reached the daemon: an unreachable one reports none.
  const version = lastGood && lastGood.ownership && lastGood.ownership.api_version;
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
  const ownership = lastGood ? lastGood.ownership : null;
  const compatibility = ownership ? ownership.compatibility : null;
  if (ownerError) {
    // The daemon answered /health, but the owner API refused or failed (Overview names the HTTP status).
    return { tone: "bad", title: "The daemon answered, but its owner API did not.",
      detail: `${ownerError[0].toUpperCase()}${ownerError.slice(1)}. Overview shows what the Hub last read.` };
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
    // The running revision comes only from the daemon's health; the Settings read's `revision` is the saved one.
    const saved = config.pending_revision;
    const known = typeof saved === "number";
    return {
      tone: "warn",
      title: "Restart needed to apply saved settings.",
      detail: known && typeof running === "number" && running < saved
        ? `Revision ${saved} is saved; the daemon still runs revision ${running}.`
        : known ? `Revision ${saved} is saved; restart the daemon to apply it.` : "",
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
