// Apps (Hub design 7): one table of every key that can reach the daemon, with readable states. A row opens its detail
// beside the list from 1200px (list and detail, the Stables desktop pattern) and in its place below that: usage tiles,
// the identity facts, session caps and retention with the daemon default as the placeholder, and Revoke in a danger
// zone. A revoked key shows its erasure deadline in its own row; restore stays on the daemon host. Every daemon string
// goes in as a text node.

import { api } from "../lib/api.mjs";
import { clear, command, dangerZone, h, icon, pill, reveal, sectionHead, table, when } from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

const POLL_MS = 30000;
const DAY = 86400;
// The daemon's connection states, as Stables labels and pill tones.
const STATES = { active: ["Active", "ok"], idle: ["Idle", "muted"], stale: ["Stale", "muted"],
  never_used: ["Never used", "muted"], revoked: ["Revoked", "bad"], erasure_pending: ["Revoked", "bad"] };
const SESSION_LABELS = { waiting_approval: "waiting approval", waiting_target: "waiting target", waiting_app: "waiting app",
  waiting_limit: "waiting limit" };

const revoked = (row) => row.revoked_at !== null || ["revoked", "erasure_pending"].includes(row.state);
const hasAppData = (row) => ["app", "device"].includes(row.kind);
const isHub = (row) => row.this_hub || row.role === "hub";
const canRevoke = (row) => Boolean(row.id) && !isHub(row) && hasAppData(row) && !revoked(row);
const manageable = (row) => row.kind === "app" && !revoked(row);
const displayName = (row) => row.this_hub ? "This Hub" : row.name || row.id;
const figure = (value) => typeof value === "number" ? value.toLocaleString() : "—";

function kindLabel(row) {
  if (isHub(row)) return "Hub";
  if (row.kind === "owner") return row.role === "runner" ? "Runner" : "Owner";
  return { app: "App", device: "Device", web: "Web" }[row.kind] || row.kind || "Unknown";
}
function stamp(seconds) {
  return typeof seconds === "number" ? new Date(seconds * 1000).toLocaleString(undefined,
    { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
}
function day(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function lastSeen(row) {
  if (typeof row.last_used_at !== "number") return "Never";
  return Date.now() / 1000 - row.last_used_at < 60 ? "Just now" : stamp(row.last_used_at);
}
function tokenAge(row) {
  if (typeof row.token_age_seconds !== "number") return "—";
  const days = Math.floor(row.token_age_seconds / DAY);
  return days === 1 ? "1 day" : `${days} days`;
}
function cost(value) {
  return typeof value === "number" ? `$${value.toFixed(2)}` : "—";
}
function sessions(row) {
  const counts = Object.entries(row.store?.sessions || {}).filter(([, value]) => typeof value === "number");
  if (!counts.length) return "None";
  const text = counts.map(([key, value]) => `${SESSION_LABELS[key] || key} ${value}`).join(" · ");
  return text[0].toUpperCase() + text.slice(1);
}

export function renderApps(main) {
  let stopped = false, busy = false, inventory = null, erasures = null, signature = "";
  let inventoryRead = null; // the inventory read in flight, which an action waits for instead of being dropped
  let selected = null; // the key whose detail is open
  let confirming = null; // "revoke" or "retention": the question asked inside the open detail (decision 5 of #65)
  let askedDays = null; // the retention the open question applies, fixed when Save retention was pressed
  let detail = null; // { id, data } or { id, error } or { id, loading }: the open App's session caps and retention
  // The open detail's fields the owner has typed in, by name. A redraw puts the same element back, so the browser keeps
  // its raw text and validity (an unfinished "1e" stays invalid instead of reading as blank).
  let draftInputs = new Map();
  let detailReadVersion = 0; // the newest caps and retention read; an older reply that lands later is dropped
  const poll = poller(() => load());
  const updated = h("span", { class: "muted refreshed" });
  const notice = h("p", { class: "notice", "aria-live": "polite", hidden: true });
  const problem = h("p", { class: "notice bad", role: "alert", hidden: true });
  const view = h("div", { class: "apps-view" });
  // Refresh shows the daemon as it is now: it reads the open App's caps and retention again and drops typed fields, so
  // a save never sends a value read (or typed) before a change made on the host.
  const refresh = h("button", { type: "button", class: "btn ghost refresh",
    onclick: () => { draftInputs = new Map(); forgetDetail(); void load(true); } }, icon("refresh"), "Refresh apps");
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Apps"), updated, refresh),
    h("p", { class: "lede" }, "Every key that can reach the daemon, and what it has used. Names marked App-supplied " +
      "come from the app itself; check the key id and origins."),
    command(["harness hub status", "harness keys list"], { parity: true }), notice, problem, view);

  function button(text, action, extra = {}) {
    return h("button", { type: "button", class: "btn", onclick: action, ...extra }, text);
  }
  function message(value, bad = false) {
    notice.textContent = value;
    notice.className = bad ? "notice bad" : "notice";
    notice.hidden = !value;
  }
  function showProblem(value) {
    problem.textContent = value;
    problem.hidden = !value;
  }
  function facts(rows) {
    return h("dl", { class: "facts" }, rows.map(([key, value]) => [h("dt", {}, key), h("dd", {}, value)]));
  }
  // Drop the open App's caps and retention, and any read of them still in flight, so the next draw reads them afresh.
  function forgetDetail() {
    detail = null;
    detailReadVersion++;
  }
  function ready() {
    return !busy && inventory?.state === "ready";
  }
  // One read at a time. A forced read (Refresh, after an action) asked for while one is in flight runs after it, so it
  // sees the daemon as it is now.
  function load(force = false) {
    if (stopped || busy) return Promise.resolve();
    if (inventoryRead) return force ? inventoryRead.then(() => load(true)) : inventoryRead;
    inventoryRead = readInventory(force).finally(() => { inventoryRead = null; });
    return inventoryRead;
  }
  async function readInventory(force) {
    poll.cancel();
    refresh.disabled = true;
    const results = await Promise.allSettled([api("apps"), api("apps/erasures")]);
    if (stopped) return;
    inventory = results[0].status === "fulfilled" ? results[0].value : null;
    erasures = results[1].status === "fulfilled" ? { rows: results[1].value } : { error: results[1].reason.message };
    showProblem(results[0].status === "rejected" ? results[0].reason.message : inventory?.error || "");
    if (results[0].status === "fulfilled") updated.textContent = `Updated ${new Date().toLocaleTimeString()} · every ${POLL_MS / 1000} s`;
    refresh.disabled = busy;
    draw(force);
    poll.schedule(POLL_MS);
  }

  function identityWarning(row, rows = inventory?.apps || []) {
    const name = (row.name || "").trim().toLowerCase();
    if (row.this_hub || !name) return null;
    if (name === "this hub") {
      return ["Reserved name", "App-supplied name uses the reserved This Hub label. Check the key id and origins."];
    }
    if (rows.some((other) => other.id !== row.id && (other.name || "").trim().toLowerCase() === name)) {
      return ["Duplicate name", "This name matches another paired key. Check the key id and origins."];
    }
    return null;
  }
  function verifiedPairing(row) {
    const matches = (inventory?.entries || []).filter((entry) => entry.verified && entry.paired.includes(row.id));
    return matches.length ? matches.map((entry) => entry.catalog_app_id).join(", ") : "No verified entry pairs this key id";
  }
  function erasureFor(row) {
    return erasures?.rows?.find((item) => item.id === row.id) || null;
  }
  function status(row) {
    if (revoked(row)) {
      if (row.erased_at !== null) return pill("Revoked · erased", "bad");
      const deadline = erasureFor(row)?.erase_after ?? row.erase_after;
      return pill(typeof deadline === "number" ? `Revoked · erases ${day(deadline)}` : "Revoked", "bad");
    }
    const [label, tone] = STATES[row.state] || ["Unknown", "muted"];
    return pill(label, tone);
  }
  // Warnings the detail spells out in full, as pills in the row.
  function flags(row, rows) {
    const warning = identityWarning(row, rows);
    return [row.errors ? pill("Errors, 24 h", "bad") : null,
      row.token_age_warning ? pill("Token 90+ days", "warn") : null, warning ? pill(warning[0], "warn") : null];
  }

  function sorted() {
    // The erasure queue is read fresh even when the inventory is a stale snapshot: a key it lists is revoked, and its
    // row shows the deadline and leads to the restore command.
    const rows = (inventory?.apps || []).map((row) => {
      const item = erasureFor(row);
      return item && !revoked(row) ? { ...row, state: "erasure_pending", revoked_at: item.revoked_at ?? row.revoked_at,
        erase_after: item.erase_after } : row;
    });
    const order = (inventory?.entries || []).flatMap((entry) => entry.paired);
    const rank = (row) => order.includes(row.id) ? order.indexOf(row.id) : order.length;
    return rows.sort((a, b) => rank(a) - rank(b));
  }
  function draw(force = true) {
    const next = JSON.stringify([inventory, erasures]);
    if (!force && next === signature) return;
    signature = next;
    // Keep the owner's focus (and caret) across a redraw.
    const active = document.activeElement;
    const focused = active?.dataset?.field ? [active.dataset.field, active.selectionStart] : null;
    const rows = sorted();
    const row = rows.find((item) => item.id === selected);
    if (!row) { selected = null; confirming = null; if (detail) forgetDetail(); }
    // An App's caps and retention are read when its detail opens, or once a refresh makes management available.
    const read = row && manageable(row) && inventory?.state === "ready" && detail?.id !== row.id;
    if (read) detail = { id: row.id, loading: true };
    const known = new Set(rows.map((item) => item.id));
    const orphans = (erasures?.rows || []).filter((item) => !known.has(item.id));
    clear(view,
      inventory?.stale ? h("p", { class: "notice warn" }, "Last good inventory from " + when(inventory.fetched_at) +
        ". App states may be out of date; management is unavailable until refresh succeeds.") : null,
      h("div", { class: row ? "apps-layout has-detail" : "apps-layout" },
        h("section", { class: "apps-keys", "aria-labelledby": "apps-keys-head" },
          sectionHead("Paired apps and keys", rows.length, { id: "apps-keys-head" }),
          rows.length ? keys(rows) : !inventory ? h("p", { class: "muted" }, "No paired app snapshot is available.") :
            h("section", { class: "card empty" }, icon("apps"), h("h3", {}, "No keys in the inventory"),
              h("p", {}, "Apps appear here once they pair. Start from Marketplace or the app's own setup screen."),
              h("a", { class: "btn", href: "#/marketplace" }, "Open Marketplace"))),
        row ? pane(row, rows) : null),
      catalog(rows),
      h("div", { class: "apps-erasures" }, erasures?.error ? h("p", { class: "notice bad", role: "alert" },
        "Erasure deadlines could not be read. " + erasures.error) : null, orphans.length ? waiting(orphans) : null));
    if (focused) focusField(focused[0], focused[1]);
    if (read) void readDetail(row.id);
  }
  function focusField(field, caret) {
    const target = view.querySelector(`[data-field="${field}"]`);
    if (!target) return;
    target.focus();
    if (typeof caret === "number" && target.setSelectionRange) target.setSelectionRange(caret, caret);
  }

  function keys(rows) {
    return h("div", { class: "card apps-table" }, table("Paired apps and keys",
      ["Name", "Kind", "Status", "Scopes", "Last seen", "Token age", h("span", { class: "visually-hidden" }, "Details")],
      rows.map((row) => {
        const name = displayName(row);
        const open = () => openDetail(row.id);
        const restore = revoked(row) && hasAppData(row) && row.erased_at === null;
        return h("tr", { "data-app-id": row.id, class: row.id === selected ? "selected" : null,
          onclick: (event) => { if (!event.target.closest("button, a")) open(); } },
        h("td", { class: "app-name" }, h("span", { class: "app-title" }, name),
          h("span", { class: "app-sub" }, h("span", { class: "mono" }, row.id),
            hasAppData(row) ? " · App-supplied name" : null),
          h("span", { class: "app-meta" }, [kindLabel(row), row.scopes || "no scopes",
            revoked(row) ? null : typeof row.last_used_at === "number" ? lastSeen(row) : "never used"]
            .filter(Boolean).join(" · "))),
        h("td", { class: "col-wide" }, kindLabel(row)),
        h("td", {}, h("span", { class: "app-status" }, status(row), flags(row, rows))),
        h("td", { class: "col-wide" }, row.scopes || "None"),
        h("td", { class: "col-wide nowrap" }, lastSeen(row)),
        h("td", { class: "col-wide nowrap" }, tokenAge(row)),
        h("td", { class: "app-open" }, restore ?
          h("button", { type: "button", class: "btn ghost", "data-field": `open-${row.id}`, onclick: open },
            "Restore command", h("span", { class: "visually-hidden" }, ` for ${name}`)) :
          h("button", { type: "button", class: "btn ghost open-app", "data-field": `open-${row.id}`, onclick: open,
            "aria-expanded": String(row.id === selected) },
          icon("chevron"), h("span", { class: "visually-hidden" }, `Open ${name}`))));
      })));
  }

  // A row's button opens its detail, and closes it again when it is already open.
  function openDetail(id) {
    if (stopped || busy) return; // an action's reset of the typed fields must not reach another App's
    if (selected === id) { closeDetail(); return; }
    draftInputs = new Map();
    forgetDetail();
    selected = id;
    confirming = null;
    draw(true);
    const heading = view.querySelector("#app-detail-title");
    heading?.focus();
    heading?.scrollIntoView?.({ block: "nearest" });
  }
  function closeDetail() {
    if (busy) return;
    const id = selected;
    selected = null; confirming = null; draftInputs = new Map();
    forgetDetail();
    draw(true);
    focusField(`open-${id}`);
  }
  async function readDetail(id) {
    const turn = ++detailReadVersion;
    try {
      const data = await api("apps/" + id + "/limits");
      if (stopped || selected !== id || turn !== detailReadVersion) return;
      detail = { id, data };
    } catch (error) {
      if (stopped || selected !== id || turn !== detailReadVersion) return;
      detail = { id, error: error.message };
    }
    draw(true);
  }

  // The open key: who it is, what it has used, its limits and what can be done to it.
  function pane(row, rows) {
    const name = displayName(row);
    const warning = identityWarning(row, rows);
    const usage = row.store?.usage || {};
    const about = [hasAppData(row) ? "App-supplied name" : null, `${kindLabel(row)} key`,
      `created ${stamp(row.created_at)}`, hasAppData(row) ? verifiedPairing(row) : null].filter(Boolean).join(" · ");
    return h("section", { class: "apps-detail", "data-detail-id": row.id, "aria-labelledby": "app-detail-title" },
      h("div", { class: "apps-detail-head" },
        h("h2", { id: "app-detail-title", tabindex: "-1", "data-field": "detail-title" }, name), status(row),
        h("span", { class: "mono muted key-id" }, row.id)),
      h("p", { class: "hint" }, about),
      button("Back to all apps", closeDetail, { class: "btn ghost apps-back" }),
      warning ? h("p", { class: "notice warn" }, warning[1]) : null,
      row.errors ? h("p", { class: "notice bad" }, "Errors in the last 24 hours" +
        (row.store?.last_error ? `: ${row.store.last_error}, ${stamp(row.store.last_error_at)}.` : ".")) : null,
      row.token_age_warning ? h("p", { class: "notice warn" }, "Token is at least 90 days old. No automatic expiry.") : null,
      hasAppData(row) ? h("div", { class: "tiles usage-tiles" },
        tile("Requests", figure(usage.requests)),
        tile("Tokens in · out", `${figure(usage.prompt_tokens)} · ${figure(usage.completion_tokens)}`),
        tile("Cost", cost(usage.cost_usd)),
        tile("Errors", figure(row.store?.errors))) : null,
      facts([["Scopes", row.scopes || "None"], ["Origins", row.origins.join(", ") || "None (native)"],
        ["Catalog app", row.catalog_app_id || "None"], ["Last seen", lastSeen(row)],
        ["Token age", typeof row.token_age_seconds === "number" ? `${tokenAge(row)} · no automatic expiry` : "—"],
        hasAppData(row) ? ["Sessions", sessions(row)] : null,
        row.store?.last_error ? ["Last error", `${row.store.last_error} · ${stamp(row.store.last_error_at)}`] : null,
        revoked(row) ? ["Revoked", stamp(row.revoked_at)] : null,
        row.erased_at !== null ? ["Erased", stamp(row.erased_at)] : null].filter(Boolean)),
      manageable(row) ? manage(row, name) : null,
      actions(row, name));
  }
  function tile(key, value) {
    return h("div", { class: "tile" }, h("p", { class: "tile-key" }, key), h("p", { class: "tile-value" }, value));
  }

  function manage(row, name) {
    if (inventory?.state !== "ready") {
      return h("p", { class: "notice warn" }, "Session caps and retention can be changed once a refresh succeeds.");
    }
    const current = detail?.id === row.id ? detail : null;
    if (!current || current.loading) return h("p", { class: "muted" }, "Reading session caps and retention…");
    // Malformed or refused limits never become editable default resets.
    if (current.error) return h("p", { class: "notice bad", role: "alert" }, current.error);
    const data = current.data;
    const running = numberField("max-running", "Maximum running (1–100)", data.configured.max_running, 100,
      data.configured.max_running === null ? `Default ${data.effective.max_running}` : "Daemon default");
    const queued = numberField("max-queued", "Maximum queued (1–1000)", data.configured.max_queued, 1000,
      data.configured.max_queued === null ? `Default ${data.effective.max_queued}` : "Daemon default");
    const retention = numberField("retention", "Retention days", data.retention_days, 36500, "Keep until deleted", "any");
    const saveCaps = button("Save session caps", () => {
      if (!running.input.reportValidity() || !queued.input.reportValidity()) return;
      // Only the caps the owner changed: the daemon keeps a field left out, so a cap changed on the host since this
      // detail was read is not written back.
      const body = {};
      for (const [key, input] of [["max_running", running.input], ["max_queued", queued.input]]) {
        if (numberValue(input) !== data.configured[key]) body[key] = numberValue(input);
      }
      if (!Object.keys(body).length) { message("Nothing to save: both caps are as the daemon has them."); return; }
      void perform(async () => {
        await api("apps/" + row.id + "/limits", { method: "PUT", body });
        message("Session caps saved.");
      });
    }, { class: "btn primary", "data-field": "save-caps", disabled: !ready() });
    const saveRetention = button("Save retention", () => {
      if (!retention.input.reportValidity() || !ready()) return;
      askedDays = numberValue(retention.input);
      confirming = "retention";
      draw(true);
      focusField("confirm-retention");
    }, { class: "btn", "data-field": "save-retention", disabled: !ready() });
    // While the question is open the field is locked: the question applies the value it names, nothing typed later.
    retention.input.disabled = confirming === "retention";
    const days = askedDays;
    return [
      sectionHead("Session caps", null, { level: 3 }),
      h("div", { class: "card apps-form" },
        h("div", { class: "apps-fields" }, running.field, queued.field),
        h("p", { class: "hint" }, `Effective caps: running ${figure(data.effective.max_running)} · queued ` +
          `${figure(data.effective.max_queued)}. Counting now: running ${figure(data.running)} · queued ` +
          `${figure(data.queued)} · parked ${figure(data.parked)}; parked sessions count toward both caps. ` +
          "Leave a field blank to use the daemon default. Saving sends only the caps you changed."),
        h("div", { class: "apps-actions" }, saveCaps),
        command(["harness apps set-limits " + row.id + " --max-running <integer> --max-queued <integer>",
          "harness apps set-limits " + row.id + " --set max_running=null --set max_queued=null"], { parity: true })),
      sectionHead("Retention", null, { level: 3 }),
      h("div", { class: "card apps-form" },
        h("div", { class: "apps-inline" }, retention.field, confirming === "retention" ? null : saveRetention),
        h("p", { class: "hint" }, "Shorter retention can erase older sessions at the next cleanup; you confirm before " +
          "it applies. Leave it blank to keep sessions until they are deleted."),
        confirming === "retention" ? ask({ title: `Change retention for ${name}?`, explanation: "Apply retention " +
          (days === null ? "until deleted" : days + " days") + ` to ${name}. Shorter retention can erase older ` +
          "sessions during cleanup.", label: `Confirm change retention for ${name}`, tone: "btn primary",
        action: async () => {
          await api("apps/" + row.id + "/retention", { method: "PUT", body: { retention_days: days, confirm: true } });
          message("Retention saved.");
        }, back: "save-retention", field: "confirm-retention" }) : null,
        command(["harness apps retention " + row.id + " --retention-days <days>",
          "harness apps retention " + row.id + " --set retention_days=null"], { parity: true }))];
  }

  // What the Hub can do to this key, and what only the daemon host can.
  function actions(row, name) {
    if (isHub(row)) {
      return h("section", { class: "card apps-host" }, h("h3", {}, "Release this Hub"),
        h("p", {}, "This Hub has no revoke control. Release it on the daemon host:"),
        command("harness hub release --confirm"));
    }
    if (!hasAppData(row)) {
      return h("p", { class: "muted apps-host-note" }, row.kind === "owner" ?
        "Admin key: revocation stops CLI or runner access at once. Manage keys on the daemon host; create a new key if revoked." :
        "Manage this key on the daemon host.");
    }
    if (revoked(row)) {
      const deadline = erasureFor(row)?.erase_after ?? row.erase_after;
      return h("section", { class: "card apps-host" }, h("h3", {}, "Restore on the daemon host"),
        row.erased_at !== null ? h("p", {}, "Data has already been erased; restore is no longer available.") :
          [h("p", {}, "Restore on the daemon host before the erasure deadline" +
            (typeof deadline === "number" ? ` (${stamp(deadline)})` : "") +
            ". The host CLI returns the replacement credential to the owner."),
          command("harness apps restore " + row.id)]);
    }
    if (!canRevoke(row)) return null;
    const cli = command("harness keys revoke " + row.id, { parity: true });
    if (confirming === "revoke") {
      const warning = identityWarning(row);
      return dangerZone("Danger zone", null, ask({ title: `Revoke ${name}?`, explanation: `Revoke access for ${name} ` +
        `(${row.id}) immediately. App and device data will be scheduled for erasure. Restore stays on the host CLI.`,
      identity: h("div", {}, h("p", {}, "App-supplied name: " + (row.name || "None")),
        warning ? h("p", { class: "notice warn" }, warning[1]) : null,
        facts([["Key id", row.id], ["Origins", row.origins.join(", ") || "None"],
          ["Catalog app", row.catalog_app_id || "None"], ["Created", stamp(row.created_at)],
          ["Verified entry pairing", verifiedPairing(row)]])),
      label: `Confirm revoke ${name}`, tone: "btn destructive", action: async () => {
        await api("apps/" + row.id + "/revoke", { method: "DELETE", body: { confirm: true } });
        message(`Revoked ${name}. Its erasure deadline now shows in its row.`);
      }, back: "revoke", field: "confirm-revoke" }), cli);
    }
    const revoke = button(`Revoke ${name}`, () => {
      if (!ready() || !canRevoke(row)) return;
      confirming = "revoke";
      draw(true);
      focusField("confirm-revoke");
    }, { class: "btn danger", "data-field": "revoke", disabled: !ready() });
    return dangerZone("Danger zone", "Revoke stops this key at once and schedules its sessions for erasure. Restore " +
      "stays on the daemon host until the deadline.", h("div", { class: "apps-actions" }, revoke), cli);
  }
  // A question asked inside the card that was acted on: role=alert, Cancel and the confirming button (label, styled
  // tone). Cancel returns focus to the back field; field names the confirming button.
  function ask({ title, explanation, identity = null, label, tone, action, back, field }) {
    const cancel = button("Cancel", () => {
      confirming = null;
      draw(true);
      focusField(back);
    }, { "data-field": "cancel" });
    const ok = button(label, () => perform(action), { class: tone, "data-field": field });
    return h("div", { class: "apps-confirm", role: "alert" }, h("h4", {}, title), h("p", {}, explanation), identity,
      h("div", { class: "apps-actions" }, cancel, ok));
  }

  function catalog(rows) {
    const entries = inventory?.entries || [];
    if (!inventory) return null;
    return h("section", { class: "apps-catalog", "aria-labelledby": "apps-catalog-head" },
      sectionHead("Catalog entries", entries.length, { id: "apps-catalog-head" }),
      entries.length ? h("div", { class: "card apps-entries" }, entries.map((entry) => {
        // The daemon's pairing ids include catalog identity AND origin checks. Names grant no association.
        const matches = rows.filter((row) => entry.paired.includes(row.id));
        return h("div", { class: "apps-entry", "data-entry-id": entry.catalog_app_id },
          h("span", { class: "apps-entry-name" }, h("strong", {}, entry.name || entry.catalog_app_id),
            h("span", { class: "muted" }, ` · ${entry.catalog_app_id}`),
            matches.length ? h("span", { class: "app-sub" }, "Paired: " + matches.map(displayName).join(", ")) : null,
            entry.verified ? null : h("span", { class: "app-sub" }, "Unsigned local entry; listing grants no permissions.")),
          matches.length ? pill("Paired", "ok") : pill("Not paired", "muted"),
          h("a", { class: "btn ghost", href: "#/marketplace" }, "Open in Marketplace"));
      })) : h("p", { class: "muted" }, "No installed entries in inventory."),
      h("p", { class: "hint" }, "An entry counts as paired only through a verified origin; a matching name grants nothing."));
  }
  // Keys waiting for erasure that the inventory no longer lists.
  function waiting(rows) {
    return [sectionHead("Waiting for erasure", rows.length), h("div", { class: "card apps-table" },
      table("Waiting for erasure", ["Name", "Kind", "Erases", "Restore"], rows.map((row) =>
        h("tr", { "data-erasure-id": row.id }, h("td", { class: "app-name" }, h("span", { class: "app-title" },
          row.name || row.id), h("span", { class: "app-sub mono" }, row.id)), h("td", {}, row.kind || "Unknown"),
        h("td", { class: "nowrap" }, stamp(row.erase_after)),
        h("td", {}, hasAppData(row) ? command("harness apps restore " + row.id) : "Manage this key on the daemon host.")))))];
  }

  async function perform(action) {
    if (busy || stopped || inventory?.state !== "ready") return;
    busy = true; poll.cancel();
    // load() redraws the list and the detail, but the header controls (Refresh, a Host commands button) stay in place:
    // whatever is disabled here that still exists after the reload is enabled again below.
    const disabled = [...main.querySelectorAll("button,input")].filter((control) => !control.disabled);
    for (const control of disabled) control.disabled = true;
    confirming = null;
    let refused = false;
    // A poll still reading finishes first; the action then runs only if management is still available.
    if (inventoryRead) await inventoryRead;
    if (stopped) return;
    try {
      if (inventory?.state !== "ready") throw new Error("The app inventory could not be refreshed.");
      await action();
      draftInputs = new Map();
    }
    catch (error) {
      if (!stopped) message(error.message + " Refresh and review the state before trying again.", true);
      refused = true;
    }
    if (stopped) return;
    busy = false;
    forgetDetail(); // a read started before the write is stale: the reload reads the caps and retention again
    await load(true);
    for (const control of disabled) control.disabled = false;
    if (refused && !stopped) reveal(notice);
  }
  function numberField(field, label, value, max, placeholder, step = "1") {
    const id = `app-${field}`;
    let input = draftInputs.get(field);
    if (input) input.setAttribute("placeholder", placeholder);
    else {
      input = h("input", { type: "number", id, min: step === "any" ? "0.000001" : "1", max, step, placeholder,
        "data-field": field, inputmode: step === "any" ? "decimal" : "numeric" });
      input.value = value === null || value === undefined ? "" : String(value);
      const typed = input;
      input.addEventListener("input", () => { draftInputs.set(field, typed); });
    }
    return { input, field: h("div", { class: "apps-field" }, h("label", { for: id }, label), input) };
  }
  function numberValue(input) { return input.value.trim() === "" ? null : Number(input.value); }
  void load(true);
  return () => { stopped = true; poll.stop(); };
}
