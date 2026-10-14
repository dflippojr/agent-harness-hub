// What the shell shows about the daemon (the rail's status chip, the Pairing badge and the banner under each page
// head), worked out from answers the pages already read: Overview's last answer, the Pairing list, the Settings
// revision, and any read the Hub refused with 503 because the daemon did not answer. It is held in memory only and
// makes no calls of its own.

// Mirrors MIN_OWNER_API in views.py; the server classifies the reported version as ownership.compatibility.
export const MIN_OWNER_API = "1.24";

let overview = null; // the last overview answer
let lastGood = null; // the last overview answer that reached the daemon
let attention = null; // { approve, confirm }: App pairing requests waiting on the owner
let config = null; // the last Settings revision read: { revision, pending_revision, restart_required }
const failures = new Map(); // path -> the Hub's message, for reads refused because the daemon did not answer
const listeners = new Set();

// api.mjs reports every GET it makes, except the session's.
export function noteRead(path, status, data) {
  const ok = status >= 200 && status < 300;
  if (status === 503) failures.set(path, (data && data.detail) || "The daemon did not answer.");
  else if (ok) failures.delete(path);
  if (ok && data && typeof data === "object") {
    if (path === "overview") {
      overview = data;
      if (data.reachable) {
        lastGood = data;
        failures.clear();
      }
      if (data.attention !== undefined) attention = data.attention;
    } else if (path === "pairing" && Array.isArray(data.requests)) {
      attention = pairingAttention(data);
    } else if (path === "settings/config") {
      config = data;
    }
  }
  for (const listener of listeners) listener();
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Forget everything (tests).
export function resetStatus() {
  overview = lastGood = attention = config = null;
  failures.clear();
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
  if (failures.size || (overview && !overview.reachable)) return { tone: "bad", text: "Daemon not answering" };
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
  if (failures.size || (overview && !overview.reachable)) {
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
  if (compatibility === "too_old") {
    return {
      tone: "bad",
      title: `This daemon's Owner API is ${ownership.api_version}; the Hub needs ${MIN_OWNER_API} or later.`,
      detail: "Update the daemon to agent-harness aa5a904 or later. Some pages will fail until you do.",
    };
  }
  if (overview && overview.restarting) {
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
