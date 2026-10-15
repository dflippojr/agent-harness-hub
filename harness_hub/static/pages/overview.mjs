// Overview: pairing requests waiting for the owner, then three status tiles (this Hub's claim, the daemon, its
// settings), the capabilities summary and the details behind them. Until the claim completes, its card leads the page.

import { api, ApiError } from "../lib/api.mjs";
import { clear, command, countdown, h, hubDownState, icon, pill, sectionHead } from "../lib/dom.mjs";
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
  let ticker = null; // the pending claim's countdown, redrawn every second between polls
  const poll = poller(load);
  const claimBox = h("div", { id: "claim", class: "claim-box" });
  const body = h("div", { class: "overview" });
  const status = h("span", { class: "muted refreshed", "aria-live": "polite" });
  const refresh = h("button", { type: "button", class: "btn ghost refresh" }, icon("refresh"), "Refresh");
  clear(main,
    h("header", { class: "page-head" }, h("h1", {}, "Overview"), status, refresh),
    claimBox, body);

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
      clear(body, error.status === 0 ? hubDownState(() => load()) :
        h("section", { class: "card bad", role: "alert" }, h("h2", {}, "Could not load the overview"), h("p", {}, error.message)));
      status.textContent = "";
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
    const claimed = data.claim.state === "claimed";
    stopTime();
    clear(claimBox, claimed ? null : claimCard(data, load, keepTime));
    const shown = data.reachable ? data : lastGood;
    // The shell's banner slot says when the daemon is not answering or restarting, or its Owner API is too old.
    // Before the claim the tiles sit under a visible Before you claim header, and say what the claim opens.
    body.className = claimed ? "overview" : "overview unclaimed";
    clear(body,
      attentionCard(data.attention),
      sectionHead(claimed ? "Status" : "Before you claim", null, { id: "status-title" }),
      h("div", { class: "tiles", role: "group", "aria-labelledby": "status-title" },
        claimed ? hubTile(data) : null,
        daemonTile(data, shown),
        shown ? settingsTile(shown, data.restarting) : null,
        claimed ? null : tile("Other sections", h("span", { class: "tile-text" }, "Open after the claim"),
          "Settings, Apps and Pairing requests need the Hub key")),
      h("div", { class: "overview-cards" },
        shown ? capabilitiesCard(shown) : null,
        detailsCard(data, shown)));
    const every = ["pending", "persisting"].includes(data.claim.state) || data.restarting ? "2 s" : "30 s";
    status.textContent = data.reachable || !lastGood
      ? `Updated ${clockTime(Date.now() / 1000)} · every ${every}`
      : `Last answer ${clockTime(lastGood.fetched_at)} · retrying every ${every}`;
  }

  function keepTime(slot, expiresAt) {
    const tick = () => clear(slot, countdown(expiresAt - Date.now() / 1000));
    tick();
    ticker = setInterval(tick, 1000);
  }

  function stopTime() {
    if (ticker !== null) clearInterval(ticker);
    ticker = null;
  }

  refresh.addEventListener("click", load);
  load();
  return () => {
    stopped = true;
    poll.stop();
    stopTime();
  };
}

// The claim's three steps, each with its label before and after it is done.
const CLAIM_STEPS = [
  ["Start the claim", "Claim started"],
  ["Check the code on the daemon host", "Code checked on the daemon host"],
  ["Approved: this Hub is the daemon's Hub", "Approved: this Hub is the daemon's Hub"],
];

function claimSteps(current) {
  return h("ol", { class: "claim-steps", "aria-label": "Claim steps" }, CLAIM_STEPS.map(([todo, done], i) => {
    if (i < current) {
      return h("li", { class: "done" }, icon("check"), done, h("span", { class: "visually-hidden" }, " (done)"));
    }
    return h("li", { class: i === current ? "current" : null, "aria-current": i === current ? "step" : null },
      h("span", { class: "step-number", "aria-hidden": "true" }, String(i + 1)), todo);
  }));
}

// Six digits in boxes, read out one by one, with a gap after the third like the daemon host's prompt.
function matchCode(code) {
  const digits = code.split("");
  return h("div", { class: "match-code", role: "img", "aria-label": `Match code ${digits.join(" ")}` },
    digits.map((digit, i) => h("span", { class: i === 3 ? "match-digit gap" : "match-digit" }, digit)));
}

// The claim card leads Overview until the claim completes. keepTime(slot, expiresAt) makes the slot a live countdown.
function claimCard(data, reload, keepTime) {
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
  // A claim needs the daemon's answer, so Start claim waits, with the reason, while it is not answering.
  const silent = !data.reachable && data.answered !== true;
  const startButton = (label) => {
    const button = h("button", { type: "button", class: "btn primary", disabled: silent,
      "aria-describedby": silent ? "claim-start-why" : null }, label);
    button.addEventListener("click", () => start(button));
    const why = silent
      ? h("p", { class: "hint", id: "claim-start-why" }, "The daemon is not answering, so a claim cannot start yet.")
      : null;
    return [h("div", { class: "claim-actions" }, button), why];
  };
  const head = (status) => h("div", { class: "claim-head" },
    h("h2", { id: "claim-title" }, "Claim this daemon"), status);
  const card = (cls, step, main, side) =>
    h("section", { class: `card claim ${cls}`.trim(), "aria-labelledby": "claim-title" },
      claimSteps(step),
      h("div", { class: side ? "claim-body two" : "claim-body" }, h("div", { class: "claim-main" }, main),
        side ? h("div", { class: "claim-side" }, side) : null));

  switch (claim.state) {
    case "persisting":
      return card("warn", 2,
        [head(pill("Waiting to save key", "warn")), h("p", { class: "lead", role: "alert" }, claim.message)],
        [h("p", { class: "label" }, "To release it, on the daemon host run"), command(claim.release_command)]);
    case "pending": {
      const offset = typeof data.fetched_at === "number" ? data.fetched_at - Date.now() / 1000 : 0;
      const left = h("span", { class: "claim-clock" });
      keepTime(left, claim.expires_at - offset);
      return card("", 1,
        [head(pill("Waiting for approval", "run")),
          h("p", { class: "lead" }, "On the daemon host, check that the code it shows matches this one, then approve."),
          h("p", { class: "label" }, "Match code"),
          matchCode(claim.match_code),
          h("div", { class: "claim-actions" }, left, forgetButton(reload))],
        [h("p", { class: "label" }, "On the daemon host, run"),
          command(claim.approve_command),
          h("p", { class: "hint" }, "Claiming makes this Hub the daemon's one admin console. This page notices the ",
            "approval by itself, also in a background tab."),
          h("p", { class: "hint" }, "Wrong code on the host? Stop waiting here and deny it there with ",
            h("span", { class: "mono" }, `harness hub deny ${claim.request_id}`), ".")]);
    }
    case "refused":
      return card("bad", 0,
        [head(pill("Already claimed", "bad")), h("p", { class: "lead" }, claim.message),
          startButton("Start a new claim")],
        [h("p", { class: "label" }, "To replace that Hub with this one, on the daemon host run"),
          command(claim.release_command),
          h("p", { class: "hint" }, "Then start a new claim here.")]);
    case "lost":
      // The daemon holds a key for this Hub that never reached it: only a release on the host frees the daemon.
      return card("bad", 0,
        [head(pill("Key lost", "bad")), h("p", { class: "lead", role: "alert" }, claim.message),
          startButton("Start a new claim")],
        [h("p", { class: "label" }, "First, on the daemon host run"),
          command(claim.release_command),
          h("p", { class: "hint" }, "Then start a new claim here.")]);
    default: {
      const unclaimed = claim.state === "unclaimed";
      const outcome = { expired: "Expired", denied: "Denied" }[claim.state];
      return card(unclaimed ? "" : "warn", 0,
        [head(outcome ? pill(outcome) : null),
          h("p", { class: "lead" }, claim.message),
          unclaimed
            ? h("p", { class: "hint" }, "Claiming makes this Hub the daemon's one admin console. You approve it on ",
              "the daemon host with harness hub approve; the Hub shows the command and its match code.")
            : null,
          startButton(unclaimed ? "Start claim" : "Start a new claim")]);
    }
  }
}

function forgetButton(reload) {
  const button = h("button", { type: "button", class: "btn text" }, "Stop waiting");
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

function clockTime(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dayTime(seconds) {
  return new Date(seconds * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// Facts joined with dots; a missing one is left out.
function line(...parts) {
  return parts.filter((part) => part !== undefined && part !== null && part !== "").join(" · ");
}

function tile(name, value, meta, ...extra) {
  const id = `tile-${name.toLowerCase().replaceAll(" ", "-")}`;
  return h("section", { class: "tile", "aria-labelledby": id },
    h("h3", { class: "tile-key", id }, name),
    h("p", { class: "tile-value" }, value),
    meta ? h("p", { class: "tile-meta" }, meta) : null,
    extra);
}

// App pairing requests the owner must decide, counted on the server. A request expires unseen if nobody looks.
function attentionCard(attention) {
  const parts = attention ? [[attention.approve, "to approve"], [attention.confirm, "to confirm"]].filter(([n]) => n > 0) : [];
  const total = parts.reduce((sum, [n]) => sum + n, 0);
  if (!total) return null;
  return h("section", { class: "needs-you", role: "status", "aria-labelledby": "attention-title" },
    h("span", { class: "needs-you-icon" }, icon("pairing")),
    h("div", { class: "needs-you-text" },
      h("h2", { id: "attention-title" }, `${total} pairing ${total === 1 ? "request needs" : "requests need"} you`),
      h("p", {}, `${parts.map(([n, label]) => `${n} ${label}`).join(" · ")}. `,
        "Apps wait on their match-code screen until you decide.")),
    h("a", { href: "#/pairing", class: "btn primary" }, "Review requests"));
}

// This Hub's claim, once complete. Ownership is what the newest answer confirmed: none while the daemon is silent.
function hubTile(data) {
  const claim = data.claim;
  const own = data.ownership;
  const name = own && own.this_hub && own.record ? own.record.name : "";
  return tile("This Hub", [pill("Claimed", "ok"), name],
    line(claim.key_id ? `Key ${claim.key_id}` : null, typeof claim.claimed_at === "number" ? `since ${dayTime(claim.claimed_at)}` : null),
    own && !own.this_hub
      ? h("p", { class: "notice bad" }, "The daemon records a different Hub. Release it on the daemon host, then claim again.")
      : null);
}

// How the Hub's minimum compares with the Owner API version the daemon reports (ownership.compatibility).
const COMPATIBILITY = {
  supported: "supported",
  no_tailnet_signin: "no tailnet sign-in",
  too_old: "too old for this Hub",
  unknown: "version not reported",
};

// Whether the daemon answers now; its release, Owner API and profile from the last answer that reached it. A daemon
// that answered /health with an error (a 503 mid-restart) is answering, as in the shell's chip.
function daemonTile(data, shown) {
  const health = shown && shown.health ? shown.health : {};
  let state = pill("Not healthy", "bad");
  if (data.reachable) {
    if (health.ok) state = pill("Healthy", "ok");
  } else if (data.answered !== true) {
    state = pill("Not answering", "bad");
  } else if (data.restarting) {
    state = pill("Restarting", "warn");
  }
  const d = shown && shown.daemon ? shown.daemon : {};
  const own = shown ? shown.ownership : null;
  const ownerApi = own ? line(own.api_version ? `Owner API ${own.api_version}` : "Owner API", COMPATIBILITY[own.compatibility]) : null;
  return tile("Daemon", [state, d.release || null], line(ownerApi, d.profile ? `profile ${d.profile}` : null));
}

function settingsTile(shown, restarting) {
  const health = shown.health || {};
  const revision = typeof health.config_revision === "number" ? `Revision ${health.config_revision}` : "Revision unknown";
  let confirmed = null;
  if (health.config_confirmed === true) confirmed = pill("Confirmed", "ok");
  else if (health.config_confirmed === false) confirmed = pill("Not confirmed", "warn");
  let restart = null;
  if (restarting) restart = "Restarting now";
  else if (health.supervised_restart === true) restart = "Restarts are supervised";
  else if (health.supervised_restart === false) restart = "Restart is manual here";
  let recovery = null;
  if (health.recovery) {
    recovery = health.recovery.recovery === "overlay_quarantined"
      ? "Managed settings quarantined; the daemon started on file defaults"
      : "The daemon started from its last good settings";
  }
  return tile("Settings", [revision, confirmed], restart, recovery ? h("p", { class: "notice warn" }, recovery) : null);
}

function plural(n, word) {
  return n === 1 ? word : `${word.replace(/y$/, "ie")}s`;
}

function capabilitiesCard(data) {
  const caps = data.capabilities || {};
  const required = Object.entries(caps.required || {});
  const modules = Object.entries(caps.modules || {});
  const missing = required.filter(([, present]) => !present).map(([name]) => name.replaceAll("_", " "));
  const on = modules.filter(([, enabled]) => enabled).length;
  // Core capabilities collapse to one line; a missing one turns it red and names it.
  const core = missing.length
    ? [icon("alert"), `${missing.length} of ${required.length} core ${plural(required.length, "capability")} missing: ${missing.join(", ")}`]
    : [icon("check"), `All ${required.length} core ${plural(required.length, "capability")} present`];
  return h("section", { class: "card", "aria-labelledby": "caps-title" },
    h("h2", { id: "caps-title" }, "Capabilities"),
    h("h3", { class: "card-sub" }, modules.length ? `Modules · ${on} of ${modules.length} on` : "Modules"),
    modules.length
      ? h("ul", { class: "chips" }, modules.map(([name, enabled]) =>
        h("li", { class: enabled ? "pill ok" : "pill muted" }, enabled ? name : `${name} · off`)))
      : h("p", { class: "muted" }, "No optional modules reported."),
    h("p", { class: "card-link" }, h("a", { href: "#/modules" }, "Manage modules")),
    h("h3", { class: "card-sub" }, "Core"),
    required.length
      ? h("details", { class: missing.length ? "core bad" : "core", open: missing.length > 0 },
        h("summary", {}, core),
        h("ul", { class: "chips" }, required.map(([name, present]) =>
          h("li", { class: present ? "pill ok" : "pill bad" }, name.replaceAll("_", " ")))))
      : h("p", { class: "muted" }, "No core capabilities reported."),
    h("h3", { class: "card-sub" }, "Hosted backends"),
    (caps.hosted_backends || []).length
      ? h("ul", { class: "chips" }, caps.hosted_backends.map((name) => h("li", { class: "pill" }, name)))
      : h("p", { class: "muted" }, "None configured."));
}

// Every fact the tiles leave out. The address and Hub version are the Hub's own, so they show even before an answer.
function detailsCard(data, shown) {
  const d = shown && shown.daemon ? shown.daemon : {};
  const health = shown && shown.health ? shown.health : {};
  const hub = data.hub || {};
  const own = data.ownership;
  const protocols = Object.entries(d.protocols || {}).map(([name, span]) => `${name} ${span.min}–${span.max}`).join(" · ");
  let recorded = "No";
  if (own && own.this_hub) recorded = own.record && own.record.name ? `Yes, as “${own.record.name}”` : "Yes";
  return h("section", { class: "card", "aria-labelledby": "details-title" },
    h("h2", { id: "details-title" }, "Details"),
    h("dl", { class: "facts" },
      fact("Daemon address", hub.daemon_url ? h("span", { class: "mono" }, hub.daemon_url) : null),
      fact("Release · build", line(d.release, d.build_id)),
      fact("Protocols", protocols),
      fact("Supervised restart", yesNo(health.supervised_restart)),
      own ? fact("Daemon records this Hub", recorded) : null,
      fact("Hub version", hub.version)));
}

function yesNo(value) {
  if (value === true) return "Yes";
  return value === false ? "No" : null;
}
