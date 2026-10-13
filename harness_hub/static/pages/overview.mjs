// Overview: this Hub's claim on the daemon, pairing requests waiting for the owner, the daemon's version, health and
// capabilities.

import { api, ApiError } from "../lib/api.mjs";
import { clear, command, h, when } from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

const REFRESH_MS = 30000;
const CLAIM_POLL_MS = 2000;
let lastGood = null; // the last overview that reached the daemon, shown with an error when a refresh fails

export function renderOverview(main) {
  let stopped = false;
  let loading = false;
  let reloadRequested = false;
  // The owner is approving on the daemon host or waiting out a restart: keep polling in a hidden tab. A failed read
  // keeps the last known answer, so one dropped request does not pause the wait.
  let whileHidden = false;
  const poll = poller(load);
  const claimBox = h("div", { id: "claim" });
  const body = h("div", { class: "grid" });
  const status = h("p", { class: "muted refreshed", "aria-live": "polite" });
  const refresh = h("button", { type: "button", class: "btn ghost" }, "Refresh");
  clear(main,
    h("header", { class: "page-head" }, h("h1", {}, "Overview"), refresh),
    claimBox, body, status);

  async function load() {
    if (stopped) return;
    if (loading) {
      reloadRequested = true;
      return;
    }
    loading = true;
    poll.cancel();
    let delay = REFRESH_MS;
    try {
      const data = await api("overview");
      if (stopped || reloadRequested) return;
      if (data.reachable) lastGood = data;
      draw(data);
      const waiting = ["pending", "persisting"].includes(data.claim.state);
      if (data.restarting) {
        await api("settings/health").catch(() => {});
        if (stopped || reloadRequested) return;
      }
      whileHidden = waiting || data.restarting;
      delay = whileHidden ? CLAIM_POLL_MS : REFRESH_MS;
    } catch (error) {
      if (stopped || reloadRequested) return;
      clear(body, h("section", { class: "card bad", role: "alert" }, h("h2", {}, "Could not load the overview"), h("p", {}, error.message)));
    } finally {
      loading = false;
      if (!stopped) {
        if (reloadRequested) {
          reloadRequested = false;
          load();
        } else {
          poll.schedule(delay, { whileHidden });
        }
      }
    }
  }

  function draw(data) {
    clear(claimBox, claimCard(data, load));
    const shown = data.reachable ? data : lastGood;
    clear(body,
      data.restarting ? h("section", { class: "card wait span", role: "status" }, h("h2", {}, "Daemon restarting"),
        h("p", {}, "Polling health until the daemon answers with confirmed settings.")) : null,
      data.error ? h("section", { class: "card bad span", role: "alert" },
        h("h2", {}, "The daemon did not answer"),
        h("p", {}, data.error),
        shown ? h("p", { class: "muted" }, `Showing what the Hub last saw, at ${when(shown.fetched_at)}.`) : null) : null,
      attentionCard(data.attention),
      shown ? daemonCard(shown) : null,
      shown ? capabilitiesCard(shown) : null);
    status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  refresh.addEventListener("click", load);
  load();
  return () => {
    stopped = true;
    poll.stop();
  };
}

function claimCard(data, reload) {
  const claim = data.claim;
  const start = async (button) => {
    button.disabled = true;
    try {
      await api("claim", { method: "POST" });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
    reload();
  };
  const startButton = (label = "Start claim") => {
    const button = h("button", { type: "button", class: "btn primary" }, label);
    button.addEventListener("click", () => start(button));
    return button;
  };
  const title = h("h2", { id: "claim-title" }, "This Hub");
  const card = (cls, ...children) => h("section", { class: `card claim ${cls}`, "aria-labelledby": "claim-title" }, title, ...children);

  switch (claim.state) {
    case "persisting":
      return card("warn",
        h("p", { class: "lead", role: "alert" }, h("span", { class: "pill wait" }, "Waiting to save key"),
          " ", claim.message),
        command(claim.release_command));
    case "claimed": {
      const own = data.ownership;
      return card("ok",
        h("p", { class: "lead" }, h("span", { class: "pill ok" }, "Claimed"), " ", claim.message),
        h("dl", { class: "facts" },
          fact("Hub key", claim.key_id),
          fact("Claimed", when(claim.claimed_at)),
          own ? fact("Daemon records this Hub", own.this_hub ? "Yes" : "No") : null,
          own && own.record ? fact("Recorded name", own.record.name) : null),
        own && !own.this_hub ? h("p", { class: "notice bad" }, "The daemon records a different Hub. Release it on the daemon host, then claim again.") : null);
    }
    case "pending": {
      const left = Math.max(0, Math.round(claim.expires_at - Date.now() / 1000));
      return card("wait",
        h("p", { class: "lead" }, h("span", { class: "pill wait" }, "Waiting for approval"), " ", claim.message),
        h("p", {}, "Match code"),
        h("p", { class: "match", "aria-label": `Match code ${claim.match_code.split("").join(" ")}` }, claim.match_code),
        h("p", {}, "On the daemon host, check that the code matches, then run:"),
        command(claim.approve_command),
        h("p", { class: "muted" }, left > 0 ? `This claim expires in about ${Math.ceil(left / 60)} min.` : "This claim is about to expire."),
        forgetButton(reload));
    }
    case "refused":
      return card("bad",
        h("p", { class: "lead" }, h("span", { class: "pill bad" }, "Already claimed"), " ", claim.message),
        h("p", {}, "To replace that Hub with this one, on the daemon host run:"),
        command(claim.release_command),
        h("p", { class: "muted" }, "Then start a new claim here."),
        startButton("Start a new claim"));
    default:
      return card(claim.state === "unclaimed" ? "" : "warn",
        h("p", { class: "lead" }, claim.message),
        claim.state === "unclaimed"
          ? h("p", { class: "muted" }, "Claiming makes this Hub the daemon's one admin console. You approve it on the daemon host with harness hub approve; the Hub shows the command and its match code.")
          : null,
        startButton(claim.state === "unclaimed" ? "Start claim" : "Start a new claim"));
  }
}

function forgetButton(reload) {
  const button = h("button", { type: "button", class: "btn ghost" }, "Stop waiting");
  button.addEventListener("click", async () => {
    button.disabled = true;
    await api("claim", { method: "DELETE" }).catch(() => {});
    reload();
  });
  return button;
}

function fact(label, value) {
  return [h("dt", {}, label), h("dd", {}, value === undefined || value === null || value === "" ? "—" : value)];
}

// Mirrors MIN_OWNER_API in views.py; the server classifies the reported version as ownership.compatibility.
const MIN_OWNER_API = "1.24";

// Advice only: the pages keep their own error handling. A supported daemon shows no notice.
function compatibilityNotice(ownership) {
  switch (ownership ? ownership.compatibility : null) {
    case "too_old":
      return h("p", { class: "notice bad", role: "alert" },
        `This daemon's Owner API (${ownership.api_version}) is older than the Hub supports (${MIN_OWNER_API}, agent-harness aa5a904). Update the daemon; some pages will fail until you do.`);
    case "no_tailnet_signin":
      return h("p", { class: "notice" }, "Tailnet sign-in needs Owner API 1.26 or later; sign-in links still work.");
    case "unknown":
      return h("p", { class: "notice" }, "This daemon did not report its Owner API version.");
    default:
      return null;
  }
}

// App pairing requests the owner must decide, counted on the server. A request expires unseen if nobody looks.
function attentionCard(attention) {
  const parts = attention ? [[attention.approve, "to approve"], [attention.confirm, "to confirm"]].filter(([n]) => n > 0) : [];
  const total = parts.reduce((sum, [n]) => sum + n, 0);
  if (!total) return null;
  return h("section", { class: "card warn span", role: "status", "aria-labelledby": "attention-title" },
    h("h2", { id: "attention-title" }, "Waiting for you"),
    h("p", { class: "lead" }, `${total} pairing ${total === 1 ? "request needs" : "requests need"} you: `,
      parts.map(([n, label]) => `${n} ${label}`).join(", ")),
    h("p", {}, h("a", { href: "#/pairing", class: "btn primary" }, "Open Pairing requests")));
}

function daemonCard(data) {
  const d = data.daemon || {};
  const health = data.health || {};
  const protocols = Object.entries(d.protocols || {}).map(([name, span]) => `${name} ${span.min}–${span.max}`).join(", ");
  return h("section", { class: "card", "aria-labelledby": "daemon-title" },
    h("h2", { id: "daemon-title" }, "Daemon"),
    h("p", { class: "lead" }, health.ok ? h("span", { class: "pill ok" }, "Healthy") : h("span", { class: "pill bad" }, "Not healthy")),
    compatibilityNotice(data.ownership),
    h("dl", { class: "facts" },
      fact("Address", data.hub.daemon_url),
      fact("Release", d.release),
      fact("Build", d.build_id),
      fact("Owner API", data.ownership ? data.ownership.api_version : null),
      fact("Profile", d.profile),
      fact("Protocols", protocols),
      fact("Settings revision", health.config_revision),
      fact("Settings confirmed", yesNo(health.config_confirmed)),
      fact("Supervised restart", yesNo(health.supervised_restart)),
      health.recovery ? fact("Recovery", health.recovery.recovery === "overlay_quarantined"
        ? "Managed settings quarantined; the daemon started on file defaults"
        : "The daemon started from its last good settings") : null,
      fact("Hub version", data.hub.version)));
}

function capabilitiesCard(data) {
  const caps = data.capabilities || {};
  const required = Object.entries(caps.required || {});
  const modules = Object.entries(caps.modules || {});
  return h("section", { class: "card", "aria-labelledby": "caps-title" },
    h("h2", { id: "caps-title" }, "Capabilities"),
    h("h3", {}, "Modules"),
    modules.length
      ? h("ul", { class: "chips" }, modules.map(([name, on]) => h("li", { class: `pill ${on ? "ok" : "off"}` }, `${name}: ${on ? "on" : "off"}`)))
      : h("p", { class: "muted" }, "No optional modules reported."),
    h("h3", {}, "Core"),
    h("ul", { class: "chips" }, required.map(([name, on]) => h("li", { class: `pill ${on ? "ok" : "bad"}` }, name.replaceAll("_", " ")))),
    h("h3", {}, "Hosted backends"),
    (caps.hosted_backends || []).length
      ? h("ul", { class: "chips" }, caps.hosted_backends.map((name) => h("li", { class: "pill" }, name)))
      : h("p", { class: "muted" }, "None."));
}

function yesNo(value) {
  return value === true ? "Yes" : value === false ? "No" : null;
}
