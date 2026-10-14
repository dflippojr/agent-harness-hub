// Spend: every App's usage and session-cap use in one sortable table, and each backend's today and 7-day usage.
// Read-only; caps change on Apps. The daemon has no per-App time windows yet (agent-harness#686), so per-App rows
// show lifetime totals. Local-model usage shows tokens only: its cost is "—", never a notional price.

import { api } from "../lib/api.mjs";
import { clear, command, h, when } from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

const POLL_MS = 60000;
const DAEMON_ISSUE = "https://github.com/dflippojr/agent-harness/issues/686";

const count = (value) => typeof value === "number" ? Math.round(value).toLocaleString() : "—";
const usd = (value) => typeof value === "number" ?
  "$" + value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: value < 1 ? 4 : 2 }) : "—";
const tokens = (usage) => typeof usage.prompt_tokens === "number" || typeof usage.completion_tokens === "number" ?
  (usage.prompt_tokens || 0) + (usage.completion_tokens || 0) : null;
const label = (row) => row.name || row.id;

// Each sortable column: its header, how a row sorts (null sorts last) and the cell it shows.
const COLUMNS = [
  { key: "name", label: "App", value: (row) => label(row).toLowerCase(),
    cell: (row) => [label(row), h("div", { class: "muted spend-id" }, row.id)] },
  { key: "requests", label: "Requests", numeric: true, value: (row) => row.usage.requests,
    cell: (row) => count(row.usage.requests) },
  { key: "tokens", label: "Tokens", numeric: true, value: (row) => tokens(row.usage),
    cell: (row) => count(tokens(row.usage)) },
  { key: "cost", label: "Cost (USD)", numeric: true, value: (row) => row.usage.cost_usd,
    cell: (row) => usd(row.usage.cost_usd) },
  { key: "caps", label: "Session caps", numeric: true, value: (row) => row.caps ? row.caps.share : null,
    cell: (row) => row.caps ? [
      `Running ${row.caps.running}/${row.caps.max_running} · queued ${row.caps.queued}/${row.caps.max_queued}`,
      row.caps.near ? [" ", h("span", { class: "pill bad" }, "Near cap")] : null] : "—" },
];

export function renderSpend(main) {
  let stopped = false, busy = false, data = null, sort = { key: "cost", descending: true };
  const poll = poller(() => load());
  const problem = h("p", { class: "notice bad", role: "alert" });
  const near = h("p", { class: "notice warn", "aria-live": "polite" });
  const apps = h("div", { class: "spend-table" });
  const backends = h("div", { class: "spend-table" });
  const refreshed = h("p", { class: "muted refreshed" });
  const refresh = h("button", { type: "button", class: "btn", onclick: () => load() }, "Refresh spend");
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Spend"), refresh),
    h("p", { class: "lead" }, "Usage across every App and backend. Read-only: change an App's session caps on Apps."),
    command("harness hub status"), problem,
    h("h2", {}, "Apps"),
    h("p", { class: "muted" }, "Lifetime totals per App. Today, 7-day and 30-day windows and a daily trend per App " +
      "wait on the daemon (", h("a", { href: DAEMON_ISSUE, rel: "noreferrer", target: "_blank" }, "agent-harness#686"),
    "). An App is flagged when its sessions reach 80% of a session cap; parked sessions count toward both caps."),
    near, apps,
    h("h2", {}, "Backends"),
    h("p", { class: "muted" }, "Today (since local midnight) and the last 7 days, across all Apps and the owner. " +
      "Local-model backends show tokens only."),
    backends, refreshed);

  async function load() {
    if (stopped || busy) return;
    busy = true;
    poll.cancel();
    refresh.disabled = true;
    try {
      data = await api("spend");
      problem.textContent = data.error ? data.error + (data.stale ?
        " Showing the last good inventory from " + when(data.fetched_at) + "." : "") : "";
    } catch (error) {
      if (stopped) return;
      problem.textContent = error.message;
    }
    if (stopped) return;
    busy = false;
    refresh.disabled = false;
    draw();
    poll.schedule(POLL_MS);
  }

  function sorted(rows) {
    const column = COLUMNS.find((c) => c.key === sort.key);
    const direction = sort.descending ? -1 : 1;
    return [...rows].sort((a, b) => {
      const x = column.value(a), y = column.value(b);
      if (x === null || x === undefined) return y === null || y === undefined ? 0 : 1;
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * direction || label(a).localeCompare(label(b));
    });
  }

  function header(column) {
    const active = sort.key === column.key;
    const toggle = () => {
      sort = { key: column.key, descending: active ? !sort.descending : column.numeric === true };
      drawApps();
      apps.querySelector(`[data-sort="${column.key}"]`)?.focus();
    };
    return h("th", { scope: "col", class: column.numeric ? "num" : null,
      "aria-sort": active ? (sort.descending ? "descending" : "ascending") : "none" },
    h("button", { type: "button", class: "sort", "data-sort": column.key, onclick: toggle },
      column.label, h("span", { "aria-hidden": "true" }, active ? (sort.descending ? " ↓" : " ↑") : "")));
  }

  function drawApps() {
    const rows = data?.apps;
    if (!Array.isArray(rows)) {
      clear(apps, h("p", { class: "muted" }, "No App usage is available."));
      clear(near);
      return;
    }
    const flagged = rows.filter((row) => row.caps?.near);
    near.textContent = flagged.length ? "Near a session cap: " + flagged.map(label).join(", ") + "." : "";
    if (!rows.length) {
      clear(apps, h("p", { class: "muted" }, "No Apps are paired."));
      return;
    }
    clear(apps, h("table", {},
      h("caption", { class: "sr-only" }, "App usage, sortable by any column"),
      h("thead", {}, h("tr", {}, COLUMNS.map(header))),
      h("tbody", {}, sorted(rows).map((row) => h("tr", { "data-app-id": row.id, class: row.caps?.near ? "near" : null },
        COLUMNS.map((column, i) => h(i ? "td" : "th", { scope: i ? null : "row", class: column.numeric ? "num" : null },
          column.cell(row))))))));
  }

  function drawBackends() {
    const rows = data?.backends || [];
    if (!rows.length) {
      clear(backends, h("p", { class: "muted" }, data?.backends_error || "No backend usage is available."));
      return;
    }
    const windows = [["today", "Today"], ["week", "7 days"]];
    clear(backends, h("table", {},
      h("caption", { class: "sr-only" }, "Backend usage today and over the last 7 days"),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Backend"),
        windows.flatMap(([, name]) => [h("th", { scope: "col", class: "num" }, name + " requests"),
          h("th", { scope: "col", class: "num" }, name + " tokens"),
          h("th", { scope: "col", class: "num" }, name + " cost")]))),
      h("tbody", {}, rows.map((row) => h("tr", { "data-backend": row.name },
        h("th", { scope: "row" }, row.name, h("div", { class: "muted spend-id" }, row.local ? "Local model" : row.billing)),
        windows.flatMap(([key]) => [h("td", { class: "num" }, count(row[key].requests)),
          h("td", { class: "num" }, count(tokens(row[key]))),
          h("td", { class: "num" }, row.local ? "—" : usd(row[key].cost_usd))]))))));
  }

  function draw() {
    drawApps();
    drawBackends();
    refreshed.textContent = data?.fetched_at ? "Inventory from " + when(data.fetched_at) + ". Refreshes every minute " +
      "while this tab is visible." : "";
  }

  load();
  return () => { stopped = true; poll.stop(); };
}
