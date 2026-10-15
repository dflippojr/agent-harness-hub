// Accounts and maintenance (Hub design 11): the six areas as a segmented control. Accounts lists the members in a table
// with readable values (sizes in GiB, dates, Enabled, Google Not linked) and opens the selected member in a detail pane
// beside it from 1200px and below it under that: the facts, Edit member and Disable, the Google sign-in actions, and the
// GitHub credential erase in a danger zone. The list and the member's detail load on their own; Add member is the
// page's primary button. Questions are asked inside the card that was acted on (decision 5 of #65). The other areas
// keep their closed catalogue of actions. What the Hub cannot do is a footnote with its host commands. Every daemon
// string goes in as a text node.

import { api } from "../lib/api.mjs";
import { clear, command, dangerZone, h, icon, pill, reveal, sectionHead, table, when } from "../lib/dom.mjs";

const GROUPS = [["accounts", "Accounts"], ["credentials", "Provider assignments"], ["skills", "Skills"],
  ["maintenance", "Maintenance"], ["runners", "Runners and projects"], ["audit", "Audit"]];
const DEFAULT = { credentials: "credentials", skills: "skills", maintenance: "maintenance", runners: "runners", audit: "audit" };
const LABELS = { login: "Exact Tailscale login", display_name: "Display name", secret_ref: "Opaque secret-file name (never a key or path)",
  enabled: "Member enabled", disk_quota_bytes: "Disk quota in bytes", max_running: "Running session cap", max_queued: "Queued session cap",
  content_hash: "Exact reviewed content hash", confirmation: "Type the phrase from the retention preview", projects: "Allowed projects (one per line; empty means no project gets it automatically)",
  models: "Allowed models (one per line)", confirmed_markers: "Checked markers (one per line)", confirmed_path: "Exact checked folder path",
  before_id: "Before audit row id", since: "Since timestamp (inclusive)", until: "Until timestamp (exclusive)", policy: "Provider policy" };
// The Google sign-in actions on a member, with the short names the detail pane shows.
const GOOGLE = [["google_invite", "Create link code"], ["google_cancel", "Cancel link code"],
  ["google_revoke", "Revoke sessions"], ["google_unlink", "Unlink"]];
// Questions whose confirming button is outlined red: they erase or end something.
const DESTRUCTIVE = new Set(["github_reset", "google_unlink", "google_revoke"]);
const UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
const targetLabel = name => ({ user_id: "Member id", pid: "Proposal id", slug: "Skill or folder slug", credential_id: "Assignment id",
  scan_id: "Scan id", candidate_id: "Candidate id", project: "Project or folder", name: "Runner name" })[name] || name;
const button = (label, action, attrs = {}) => h("button", { type: "button", class: "btn", onclick: action, ...attrs }, label);
const validTarget = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(value) && !/(?:^|[^A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_-]{4,}/.test(value);
const validHash = value => typeof value === "string" && /^[a-fA-F0-9]{64}$/.test(value);
const readable = value => String(value).replaceAll("_", " ");
// An action label inside a sentence: "Erase GitHub credential" reads "erase GitHub credential".
const lower = label => label.charAt(0).toLowerCase() + label.slice(1);
const figure = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—";
const memberName = row => row.display_name || row.login || row.user_id || "Unnamed member";

// A byte count in binary units: "0 B", "312 MiB", "20 GiB".
export function size(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "—";
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) { value /= 1024; unit++; }
  const shown = unit && value < 10 ? Math.round(value * 10) / 10 : Math.round(value);
  return `${shown.toLocaleString()} ${UNITS[unit]}`;
}
// The daemon's epoch seconds as a short local date and time: "13 Oct, 12:58".
export function stamp(seconds) {
  return typeof seconds === "number" && Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString(undefined,
    { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
}
function enabledPill(row) {
  if (row.enabled === true) return pill("Enabled", "ok");
  return row.enabled === false ? pill("Disabled", "muted") : pill("Unknown", "muted");
}
function sessions(row) { return `${figure(row.running)} running · ${figure(row.queued)} queued`; }
function caps(row) { return `${figure(row.max_running)} running · ${figure(row.max_queued)} queued`; }
function disk(row) { return `${size(row.disk_used_bytes)} of ${size(row.disk_quota_bytes)}`; }
function googleFact(row) {
  const google = row.google || {};
  const count = google.active_web_sessions;
  const parts = [google.linked === true ? "Linked" + (google.email ? ` as ${google.email}` : "") : "Not linked",
    typeof count === "number" ? `${figure(count)} web ${count === 1 ? "session" : "sessions"}` : null];
  if (typeof google.invitation_expires_at === "number" && google.invitation_expires_at * 1000 > Date.now()) {
    parts.push(`link code open until ${stamp(google.invitation_expires_at)}`);
  }
  return parts.filter(Boolean).join(" · ");
}
function quotaHint(value) {
  const bytes = Number(value);
  return String(value).trim() && Number.isFinite(bytes) && bytes >= 0 ? `That is ${size(bytes)}.` : "Whole bytes; 1 GiB is 1073741824.";
}
function shown(name, value) {
  if (value === undefined || value === null || value === "") return "None";
  return name === "disk_quota_bytes" && typeof value === "number" ? size(value) : String(value);
}

export function renderAdmin(main) {
  let stopped = false, busy = false, catalogue = [], active = "accounts", selected, codeTimer, preview = null, reviewedProposal = null;
  // Accounts: the member list, the open member and its detail read, and what the detail pane is doing.
  let members = null, membersError = "", memberId = null, detail = null, accountsRead = 0, memberRead = 0;
  let paneMode = null; // "add" or "edit"
  let asking = null; // { place, op, targets, body, rows, back }: a question asked inside the card that was acted on
  let linkCode = null; // { id, code, expires_at }: the Google link code just created, shown once
  // The fields the owner has typed in, by form and name. A redraw puts the same element back, so typing survives a read.
  let drafts = new Map();
  const notice = h("p", { class: "notice", role: "status", "aria-live": "polite" });
  const navigation = h("div", { class: "segmented", role: "group", "aria-label": "Area" });
  const accountsView = h("div", { class: "admin-accounts" });
  const workspace = h("section", { class: "card admin-workspace", hidden: true });
  const output = h("section", { class: "admin-output", "aria-live": "polite" });
  const excluded = h("p", { class: "hint admin-footnote" });
  const refresh = h("button", { type: "button", class: "btn ghost refresh", onclick: () => chooseGroup(active) },
    icon("refresh"), "Refresh area");
  const add = h("button", { type: "button", class: "btn primary", disabled: true, onclick: () => addMember() }, "Add member");
  clear(main, h("header", { class: "page-head admin-head" }, h("h1", {}, "Accounts and maintenance"), refresh, add),
    navigation, notice, accountsView, workspace, output, excluded);

  const opFor = id => catalogue.find(op => op.id === id);
  const cli = (op, id) => op.cli.replace("<user_id>", id);
  function eraseOutput() { clear(output); }
  function eraseCode() { clearTimeout(codeTimer); linkCode = null; }
  function message(text, bad = false) { notice.textContent = text; notice.className = bad ? "notice bad" : "notice"; }
  function setBusy(value) {
    busy = value;
    for (const control of main.querySelectorAll("button, input, select, textarea")) control.disabled = value;
  }
  function chooseGroup(group) {
    if (busy || stopped) return;
    active = group;
    preview = null; reviewedProposal = null; eraseOutput(); eraseCode();
    for (const control of navigation.children) control.setAttribute("aria-pressed", String(control.dataset.group === group));
    accountsView.hidden = group !== "accounts";
    workspace.hidden = group === "accounts";
    if (group === "accounts") {
      paneMode = null; asking = null; drafts = new Map(); clear(workspace);
      drawAccounts(); void loadAccounts();
      return;
    }
    clear(accountsView);
    selected = opFor(DEFAULT[group]); drawForm(); void execute(selected, {}, {});
  }

  // Accounts: the members table and the open member's detail pane.
  async function loadAccounts() {
    const turn = ++accountsRead;
    let rows = null, failure = "";
    try { rows = await api("manage/accounts"); } catch (error) { failure = error.message; }
    if (stopped || turn !== accountsRead) return;
    members = failure ? null : Array.isArray(rows) ? rows : [];
    membersError = failure;
    if (!members?.some(row => row.user_id === memberId)) memberId = members?.find(row => validTarget(row.user_id))?.user_id ?? null;
    detail = null; memberRead++; // a detail read started before this list is dropped; the open member is read again
    drawAccounts();
    if (memberId && active === "accounts") void loadMember(memberId);
  }
  async function loadMember(id) {
    const turn = ++memberRead;
    try {
      const data = await api("manage/account?" + new URLSearchParams({ user_id: id }));
      if (stopped || turn !== memberRead || memberId !== id) return;
      detail = { id, data: data && typeof data === "object" ? data : {} };
    } catch (error) {
      if (stopped || turn !== memberRead || memberId !== id) return;
      detail = { id, error: error.message };
    }
    drawAccounts();
  }
  function current() {
    const row = members?.find(item => item.user_id === memberId);
    if (!row) return null;
    return detail?.id === memberId && detail.data ? { ...row, ...detail.data, user_id: row.user_id } : row;
  }
  function selectMember(id) {
    if (busy || stopped || !validTarget(id)) return;
    if (id !== memberId) { eraseCode(); detail = null; drafts = new Map(); memberId = id; void loadMember(id); }
    paneMode = null; asking = null;
    drawAccounts();
    focusField("member-title");
  }
  function addMember() {
    if (busy || stopped || !opFor("account_add")) return;
    if (active !== "accounts") chooseGroup("accounts");
    eraseCode(); paneMode = "add"; asking = null; drafts = new Map();
    drawAccounts();
    focusField("add:login");
  }
  function drawAccounts() {
    if (active !== "accounts") return;
    // Keep the owner's focus across a redraw: the same field, or the control that took its place.
    const focused = document.activeElement?.dataset?.field;
    const caret = focused ? caretOf(document.activeElement) : null;
    const pane = paneMode === "add" ? addPane() : memberPane();
    clear(accountsView, h("div", { class: pane ? "admin-accounts-layout has-detail" : "admin-accounts-layout" },
      h("section", { class: "admin-members", "aria-labelledby": "admin-members-head" },
        sectionHead("Members", members ? members.length : null, { id: "admin-members-head" }),
        membersError ? h("p", { class: "notice bad", role: "alert" }, "Members could not be read. " + membersError) :
          !members ? h("p", { class: "muted" }, "Reading members…") : members.length ? memberTable() :
            h("section", { class: "card empty" }, icon("admin"), h("h3", {}, "No members yet"),
              h("p", {}, "Add a household member by their exact Tailscale login.")),
        h("div", { class: "admin-actions" }, opFor("account_audit") ?
          button("Household audit", () => execute(opFor("account_audit"), {}, {}), { class: "btn ghost", "data-field": "account-audit" }) : null),
        opFor("accounts") ? command(opFor("accounts").cli, { parity: true }) : null),
      pane));
    if (busy) for (const control of accountsView.querySelectorAll("button, input")) control.disabled = true;
    if (focused) focusField(focused, caret);
  }
  function memberTable() {
    return h("div", { class: "card admin-table" }, table("Members",
      ["Member", "Status", "Sessions", "Disk", "Google", h("span", { class: "visually-hidden" }, "Details")],
      members.map(row => {
        const id = row.user_id, name = memberName(row), usable = validTarget(id);
        const open = () => selectMember(id);
        const google = row.google?.linked === true ? pill("Linked", "ok") : h("span", { class: "muted nowrap" }, "Not linked");
        return h("tr", { "data-user-id": usable ? id : null, class: usable && id === memberId && paneMode !== "add" ? "selected" : null,
          onclick: usable ? event => { if (!event.target.closest("button, a")) open(); } : null },
        h("td", { class: "member-name" }, h("span", { class: "member-title" }, name),
          h("span", { class: "member-sub" }, row.login || "No login"),
          h("span", { class: "member-meta" }, `${sessions(row)} · ${disk(row)}`)),
        h("td", {}, enabledPill(row)),
        h("td", { class: "col-wide" }, sessions(row), h("span", { class: "member-sub" }, `caps ${figure(row.max_running)} · ${figure(row.max_queued)}`)),
        h("td", { class: "col-wide nowrap" }, size(row.disk_used_bytes), h("span", { class: "member-sub" }, `of ${size(row.disk_quota_bytes)}`)),
        h("td", { class: "col-wide" }, google),
        h("td", { class: "member-open" }, usable ? h("button", { type: "button", class: "btn ghost open-member",
          "data-field": `open-${id}`, "aria-expanded": String(id === memberId && paneMode !== "add"), onclick: open },
        icon("chevron"), h("span", { class: "visually-hidden" }, `Open ${name}`)) : null));
      })));
  }
  function facts(rows) {
    return h("dl", { class: "facts" }, rows.map(([key, value]) => [h("dt", {}, key), h("dd", {}, value)]));
  }
  // An action on the open member: the question first when the daemon wants one, otherwise straight to the daemon.
  function actionButton(id, label, place, extra = {}) {
    const op = opFor(id), row = current();
    if (!op || !row) return null;
    const field = `action-${id}`;
    return button(label, () => op.confirmation ?
      ask(place, op, { user_id: row.user_id }, {}, [["Member", `${memberName(row)} (${row.user_id})`]], field) :
      void run(op, { user_id: row.user_id }, {}), { "data-field": field, ...extra });
  }
  function memberPane() {
    const row = current();
    if (!row) return null;
    const id = row.user_id, name = memberName(row), save = opFor("account_save");
    const google = GOOGLE.map(([op, label]) => actionButton(op, label, "google", { class: "btn small" })).filter(Boolean);
    const erase = opFor("github_reset");
    return h("section", { class: "card admin-member", "aria-labelledby": "admin-member-title", "data-member-id": id },
      h("div", { class: "admin-member-head" },
        h("h2", { id: "admin-member-title", tabindex: "-1", "data-field": "member-title" }, name), enabledPill(row)),
      h("p", { class: "hint" }, row.login ? `${row.login} · ` : null, h("span", { class: "mono" }, id)),
      detail?.id === id && detail.error ? h("p", { class: "notice bad", role: "alert" },
        "The member's detail could not be read; this shows the list's values. " + detail.error) : null,
      facts([["Created", stamp(row.created_at)],
        ["Last activity", typeof row.last_activity_at === "number" ? stamp(row.last_activity_at) : "None yet"],
        ["Sessions", sessions(row)], ["Caps", caps(row)], ["Disk", disk(row)], ["Google", googleFact(row)],
        row.google?.linked === true ? ["Last Google sign-in", typeof row.google.last_sign_in_at === "number" ?
          stamp(row.google.last_sign_in_at) : "Never"] : null].filter(Boolean)),
      save ? [paneMode === "edit" ? editForm(row, save) : h("div", { class: "admin-actions" },
        button("Edit member", () => openEdit(), { "data-field": "edit" }),
        button(row.enabled === true ? "Disable" : "Enable", () => ask("member", save, { user_id: id },
          { enabled: row.enabled !== true }, [["Member", `${name} (${id})`],
            ["Status", `${row.enabled === true ? "Enabled" : "Disabled"} → ${row.enabled === true ? "Disabled" : "Enabled"}`]], "toggle"),
        { "data-field": "toggle" })),
      asking?.place === "member" ? question() : null] : null,
      command([opFor("account"), save].filter(Boolean).map(op => cli(op, id)), { parity: true, key: "admin-member" }),
      google.length ? [h("h3", { class: "admin-sub" }, "Google sign-in"), h("div", { class: "admin-actions" }, google),
        linkCodeBox(id), asking?.place === "google" ? question() : null,
        command(GOOGLE.map(([op]) => opFor(op)).filter(Boolean).map(op => cli(op, id)), { parity: true, key: "admin-google" })] : null,
      erase ? dangerZone("Danger zone", asking?.place === "danger" ? null :
        "Erase the GitHub credential stored for this member. The Hub asks you to confirm first.",
      asking?.place === "danger" ? question() : h("div", { class: "admin-actions" },
        actionButton("github_reset", "Erase GitHub credential", "danger", { class: "btn danger" })),
      command(cli(erase, id), { parity: true, key: "admin-github" })) : null);
  }
  function linkCodeBox(id) {
    if (linkCode?.id !== id) return null;
    return h("div", { class: "admin-link-code", role: "status" },
      h("p", {}, h("strong", {}, "Google link code, shown once. "),
        "Give it to the member. It disappears when you leave this member or this page, and expires " +
        stamp(linkCode.expires_at) + "."),
      h("code", { class: "admin-code", "data-link-code": true }, linkCode.code),
      button("Dismiss code", () => { eraseCode(); drawAccounts(); focusField("action-google_invite"); }, { "data-field": "dismiss-code" }));
  }
  function showCode(id, reply) {
    eraseCode();
    if (typeof reply?.code !== "string" || !reply.code) return;
    linkCode = { id, code: reply.code, expires_at: reply.expires_at };
    const left = typeof reply.expires_at === "number" ? reply.expires_at * 1000 - Date.now() : 0;
    codeTimer = setTimeout(() => { linkCode = null; drawAccounts(); }, Math.max(0, Math.min(3600000, left)));
  }

  // A form field the owner types in, kept across redraws by form and name.
  function field(form, name, kind, value, required = false) {
    const key = `${form}:${name}`, id = `admin-${form}-${name}`;
    let input = drafts.get(key);
    if (!input) {
      const number = kind === "integer";
      input = h("input", { id, name, type: number ? "number" : "text", min: number ? 1 : undefined, step: number ? 1 : undefined,
        max: number ? Number.MAX_SAFE_INTEGER : undefined, inputmode: number ? "numeric" : undefined, required,
        autocomplete: "off", "data-field": key });
      input.value = value === undefined || value === null ? "" : String(value);
      input.dataset.initial = input.value;
      if (name === "disk_quota_bytes") input.addEventListener("input", () => {
        const hint = input.parentElement?.querySelector(".hint");
        if (hint) hint.textContent = quotaHint(input.value);
      });
      drafts.set(key, input);
    }
    return h("div", { class: "admin-field" }, h("label", { for: id }, (LABELS[name] || readable(name)) + (required ? " *" : "")),
      input, name === "disk_quota_bytes" ? h("p", { class: "hint" }, quotaHint(input.value)) : null);
  }
  // The typed values of a form's fields: text trimmed, whole numbers as numbers. A blank optional number is left out.
  function typed(form, specs) {
    const values = {};
    for (const spec of specs) {
      const input = drafts.get(`${form}:${spec.name}`);
      if (!input) continue;
      if (!input.reportValidity()) return null;
      const raw = input.value.trim();
      if (spec.type === "integer") { if (raw !== "") values[spec.name] = Number(raw); }
      else values[spec.name] = raw;
    }
    return values;
  }
  function addPane() {
    const op = opFor("account_add");
    if (!op) return null;
    const form = h("form", { class: "admin-member-form", onsubmit: event => {
      event.preventDefault();
      const values = typed("add", op.fields);
      if (!values) return;
      const body = Object.fromEntries(Object.entries(values).filter(([name, value]) => value !== "" ||
        op.fields.find(spec => spec.name === name)?.required));
      void run(op, {}, body);
    } }, h("div", { class: "admin-fields" }, op.fields.map(spec => field("add", spec.name, spec.type, "", spec.required))),
    h("div", { class: "admin-actions" }, h("button", { type: "submit", class: "btn primary", "data-field": "add-submit" }, "Add member"),
      button("Cancel", () => { paneMode = null; drafts = new Map(); drawAccounts(); focusField("member-title"); }, { "data-field": "add-cancel" })));
    return h("section", { class: "card admin-member", "aria-labelledby": "admin-add-title" },
      h("div", { class: "admin-member-head" }, h("h2", { id: "admin-add-title" }, "Add member")),
      h("p", { class: "hint" }, "Use the member's exact Tailscale login. Optional fields left blank take the daemon's defaults."),
      form, command(op.cli, { parity: true }));
  }
  function editFields(op) { return op.fields.filter(spec => spec.name !== "enabled"); }
  function openEdit() {
    if (busy) return;
    paneMode = "edit"; asking = null; drafts = new Map();
    drawAccounts();
    focusField("edit:display_name");
  }
  function editForm(row, op) {
    const form = h("form", { class: "admin-member-form", onsubmit: event => { event.preventDefault(); saveEdit(row, op); } },
      h("div", { class: "admin-fields" }, editFields(op).map(spec => field("edit", spec.name, spec.type, row[spec.name]))),
      h("p", { class: "hint" }, "Saving sends only the fields you changed, after you confirm them."),
      h("div", { class: "admin-actions" }, h("button", { type: "submit", class: "btn primary", "data-field": "edit-save" }, "Save changes"),
        button("Cancel", () => { paneMode = null; asking = null; drafts = new Map(); drawAccounts(); focusField("edit"); }, { "data-field": "edit-cancel" })));
    return form;
  }
  // Only the fields the owner changed go in the question and the write. A field left as it was filled is never sent,
  // even when a later read shows the daemon's value has moved on (a login rebound on the host stays rebound).
  function saveEdit(row, op) {
    const body = {}, changes = [];
    for (const spec of editFields(op)) {
      const input = drafts.get(`edit:${spec.name}`);
      if (!input) continue;
      if (!input.reportValidity()) return;
      const raw = input.value.trim(), label = LABELS[spec.name] || readable(spec.name);
      if (raw === input.dataset.initial.trim()) continue;
      if (raw === "") { message(`${label} cannot be empty.`, true); return; }
      const value = spec.type === "integer" ? Number(raw) : raw;
      if (value === row[spec.name]) continue;
      body[spec.name] = value;
      changes.push([label, `${shown(spec.name, row[spec.name])} → ${shown(spec.name, value)}`]);
    }
    if (!changes.length) { message("Nothing to save: every field matches the daemon's value."); return; }
    ask("member", op, { user_id: row.user_id }, body, [["Member", `${memberName(row)} (${row.user_id})`], ...changes], "edit-save");
  }
  function ask(place, op, targets, body, rows, back) {
    if (busy || stopped) return;
    asking = { place, op, targets, body, rows, back };
    drawAccounts();
    focusField("confirm");
  }
  // The question inside the card that was acted on: role=alert, what changes, Cancel and the confirming button.
  function question() {
    const { op, targets, body, rows, back } = asking;
    const name = "Confirm " + lower(op.label);
    const destructive = DESTRUCTIVE.has(op.id) || body.enabled === false;
    return h("div", { class: "admin-confirm", role: "alert", "aria-label": "Confirm action" },
      h("h4", {}, name + "?"), op.confirmation ? h("p", {}, op.confirmation) : null, facts(rows),
      h("div", { class: "admin-actions" },
        button("Cancel", () => { asking = null; drawAccounts(); focusField(back); }, { "data-field": "cancel" }),
        button(name, () => void run(op, targets, { ...body, confirm: true }),
          { class: destructive ? "btn danger" : "btn primary", "data-field": "confirm" })));
  }
  // A write on Accounts: the daemon's answer goes in the page notice, then the list and the open member are read again.
  async function run(op, targets, body) {
    if (busy || stopped) return;
    asking = null; drawAccounts(); setBusy(true); message("Applying " + lower(op.label) + "…");
    const query = new URLSearchParams(targets);
    let refused = false;
    try {
      const reply = await api("manage/" + op.id + (query.size ? "?" + query : ""), { method: op.method, body });
      if (stopped) return;
      if (op.id === "google_invite") showCode(targets.user_id, reply);
      // A cancelled or unlinked code can no longer be redeemed, so it is no longer shown for sharing.
      if ((op.id === "google_cancel" || op.id === "google_unlink") && linkCode?.id === targets.user_id) eraseCode();
      if (op.id === "account_add" && validTarget(reply?.user_id)) { memberId = reply.user_id; detail = null; }
      if (op.id === "account_add" || op.id === "account_save") { paneMode = null; drafts = new Map(); }
      message("Daemon accepted " + lower(op.label) + ".");
    } catch (error) {
      if (stopped) return;
      refused = true;
      message(error.message + " Inspect current state before repeating a write.", true);
    }
    await loadAccounts();
    if (stopped) return;
    setBusy(false);
    if (refused) reveal(notice);
    else focusField(op.id === "google_invite" ? "dismiss-code" : "member-title");
  }
  function focusField(name, caret = null) {
    const target = accountsView.querySelector(`[data-field="${name}"]`);
    if (!target || target === document.activeElement) return;
    target.focus();
    // Number inputs have no caret to put back; only text fields take one.
    if (typeof caret === "number" && target.type === "text") target.setSelectionRange(caret, caret);
  }
  function caretOf(element) {
    return element.type === "text" ? element.selectionStart : null;
  }

  // The other areas: the area's actions in a closed catalogue, each with its form, and the result below.
  function chooseOperation(id, targets = {}, values = {}) {
    if (busy || stopped) return;
    if (id !== "skill_install") reviewedProposal = null;
    eraseOutput(); selected = opFor(id); drawForm(targets, values);
  }
  function inputField(name, kind, required, value, isTarget = false) {
    const attrs = { name, required, "data-kind": kind, autocomplete: "off" };
    let input;
    if (kind === "boolean") input = h("select", attrs, h("option", { value: "" }, "Keep unchanged"),
      h("option", { value: "true" }, "Enabled"), h("option", { value: "false" }, "Disabled"));
    else if (name === "policy") input = h("select", attrs, ["api_key", "subscription", "subscription_then_api_key"].map(v => h("option", { value: v }, readable(v))));
    else if (kind === "list") input = h("textarea", { ...attrs, required: false, rows: 3 });
    else input = h("input", { ...attrs, type: ["integer", "number"].includes(kind) ? "number" : "text",
      min: kind === "integer" ? 1 : undefined, step: kind === "integer" ? 1 : "any" });
    if (value !== undefined && value !== null) input.value = Array.isArray(value) ? value.join("\n") : String(value);
    if (name === "confirmation") input.value = ""; // The owner types the daemon phrase; never prefill it.
    const label = isTarget ? targetLabel(name) : LABELS[name] || readable(name);
    return h("div", { class: "setting-field" }, h("label", {}, label + (required ? " *" : ""), input));
  }
  function drawForm(targets = {}, values = {}) {
    const choices = h("select", { "aria-label": "Choose action", onchange: event => chooseOperation(event.target.value) },
      catalogue.filter(op => op.group === active).map(op => h("option", { value: op.id }, op.label)));
    choices.value = selected.id;
    const form = h("form", { class: "admin-form", onsubmit: event => {
      event.preventDefault(); if (busy) return;
      const target = {}, body = {};
      try {
        for (const field of form.querySelectorAll("[name]")) {
          if (selected.targets.includes(field.name)) { target[field.name] = field.value.trim(); continue; }
          const spec = selected.fields.find(spec => spec.name === field.name);
          if (!field.value && !spec.required && spec.type !== "list") continue;
          body[field.name] = spec.type === "boolean" ? field.value === "true" : ["integer", "number"].includes(spec.type) ?
            Number(field.value) : spec.type === "list" ? field.value.split("\n").map(v => v.trim()).filter(Boolean) : field.value;
        }
        if (selected.id === "skill_install" && (!reviewedProposal || reviewedProposal.id !== target.pid ||
          reviewedProposal.content_hash !== body.content_hash)) throw Error("Read the complete proposal at this hash first, or review on the host CLI.");
        if (selected.id === "retention_apply" && !preview) throw Error("Read a fresh retention preview before applying it.");
        if (selected.id === "skill_projects") confirmProjects(selected, target, body);
        else if (selected.confirmation) confirmAction(selected, target, body); else execute(selected, target, body);
      } catch (error) { message(error.message, true); }
    } });
    form.append(...selected.targets.map(name => inputField(name, "text", true, targets[name], true)),
      ...selected.fields.map(field => inputField(field.name, field.type, field.required, values[field.name])));
    if (selected.id === "credential_set") form.append(h("p", { class: "muted" },
      "The daemon maps this opaque name to an owner-configured file. The Hub never opens that file or accepts a credential value. Subscription policy may use an empty name."));
    if (selected.id === "retention_apply") form.append(h("p", { class: "notice warn" }, preview ?
      "Preview: " + preview.count + " images, " + preview.bytes + " bytes. Exact phrase: " + preview.confirmation : "Preview retention in this area first."));
    if (selected.id === "folder_trust") form.append(h("p", {}, "The trust prompt opens on the daemon host. Complete the review on that device."));
    form.append(h("button", { type: "submit", class: "btn" }, selected.label));
    clear(workspace, h("h2", {}, GROUPS.find(([id]) => id === active)[1]),
      h("div", { class: "setting-field" }, h("label", {}, "Choose action", choices)),
      form, command(selected.cli, { parity: true }), h("div", { class: "admin-confirmation" }));
  }
  async function confirmProjects(op, targets, body) {
    setBusy(true); message("Reading the current project allowlist…");
    try {
      const skills = await api("manage/skills");
      if (stopped) return;
      const skill = skills.installed?.find(row => row.slug === targets.slug);
      if (!skill || skill.projects_complete !== true || !Array.isArray(skill.projects))
        throw Error("The current project list is unavailable. Review on the host CLI before changing it.");
      confirmAction(op, targets, body, { before_projects: skill.projects, after_projects: body.projects,
        added_projects: body.projects.filter(project => !skill.projects.includes(project)) });
      message("Check the before and after project lists before confirming.");
    } catch (error) { if (!stopped) message(error.message, true); }
    finally { if (!stopped) setBusy(false); }
  }
  function confirmAction(op, targets, body, detail = {}) {
    const slot = workspace.querySelector(".admin-confirmation");
    const confirm = button("Confirm " + lower(op.label), () => {
      clear(slot); execute(op, targets, { ...body, confirm: true });
    });
    clear(slot, h("section", { class: "notice warn", role: "alert", "aria-label": "Confirm action" },
      h("h3", {}, "Confirm " + lower(op.label)), h("p", {}, op.confirmation),
      h("pre", { class: "admin-text" }, JSON.stringify({ ...targets, ...body, ...detail }, null, 2)),
      h("div", { class: "settings-actions" }, confirm, button("Cancel", () => clear(slot)))));
    confirm.focus();
  }
  async function execute(op, targets, body) {
    if (busy || stopped) return;
    eraseOutput(); setBusy(true); message("Reading or applying " + lower(op.label) + "…");
    const query = new URLSearchParams(targets);
    if (op.method === "GET") for (const [key, value] of Object.entries(body)) query.set(key, String(value));
    try {
      const reply = await api("manage/" + op.id + (query.size ? "?" + query : ""),
        op.method === "GET" ? {} : { method: op.method, body });
      if (stopped) return;
      if (op.id === "proposal") reviewedProposal = reply.review_complete === true ? reply : null;
      if (op.id === "skill_install") reviewedProposal = null;
      if (op.id === "retention_preview") preview = reply;
      if (op.id === "retention_apply") preview = null;
      message(op.method === "GET" ? "Current daemon state." : "Daemon accepted " + lower(op.label) + ". Read current state before another change.");
      showResult(op, reply);
    } catch (error) {
      if (!stopped) message(error.message + " Inspect current state before repeating a write.", true);
      if (op.id === "retention_preview" || op.id === "retention_apply") preview = null;
    } finally { if (!stopped) setBusy(false); }
  }
  function record(value) {
    if (value === null || value === undefined) return h("p", { class: "muted" }, "Unavailable");
    if (typeof value !== "object") return h("span", {}, String(value));
    if (Array.isArray(value)) return value.length ? h("div", {}, value.map(record)) : h("p", { class: "muted" }, "None");
    return h("dl", { class: "facts module-facts" }, Object.entries(value).map(([key, item]) => [h("dt", {}, readable(key)),
      h("dd", {}, ["skill_md", "diff", "content", "summary", "prompt", "expected", "expected_behavior"].includes(key) ?
        h("pre", { class: "admin-text" }, String(item || "")) : record(item))]));
  }
  function rowActions(op, row) {
    const ids = op.id === "credentials" ? ["credential_revoke"] : op.id === "runners" ? ["runner_update"] :
      op.id === "proposal" ? ["skill_install", "skill_reject", "skill_reopen", "skill_review", "proposal_delete"] :
      op.id === "remote" ? ["folder_trust", ...(row.managed ? ["folder_forget"] : [])] :
      op.id === "skills" && Object.hasOwn(row, "id") ? ["proposal"] : op.id === "skills" || op.id === "skills_enabled" ?
      ["skill_enable", "skill_disable", "skill_projects", "skill_rollback", "skill_uninstall", "skill_export"] : [];
    return h("div", { class: "settings-actions" }, ids.flatMap(id => {
      const action = opFor(id);
      if (!action) return [];
      const targets = Object.fromEntries(action.targets.map(name => [name, ({ pid: row.id, credential_id: row.id,
        user_id: row.user_id, slug: row.slug || row.project, project: row.project, name: row.name })[name]]));
      if (Object.values(targets).some(value => !validTarget(value)) ||
        id === "skill_install" && (row.review_complete !== true || !validHash(row.content_hash))) return [];
      return button(action.label, () => chooseOperation(id, targets, id === "skill_install" ? { content_hash: row.content_hash } :
        id === "skill_projects" ? { projects: row.projects_complete === true ? row.projects : [] } : {}));
    }));
  }
  // Audit rows carry the daemon's epoch seconds in ts; show the owner a readable time.
  function withTime(row) { return typeof row.ts === "number" ? { ...row, ts: when(row.ts) } : row; }
  function showResult(op, reply) {
    clear(output, h("h2", {}, op.label));
    let rows = Array.isArray(reply) ? reply : op.id === "skills" ? [...(reply.proposals || []), ...(reply.installed || [])] :
      op.id === "remote" ? reply.projects || [] : op.id === "audit" ? reply.items || [] : null;
    if (rows) {
      if (!rows.length) output.append(h("p", { class: "muted" }, "No records."));
      for (const row of rows) output.append(h("section", { class: "card admin-row" },
        h("h3", {}, row.display_name || row.title || row.name || row.project || row.action || row.id || "Record"),
        record(op.id === "audit" || op.id === "account_audit" ? withTime(row) : row), rowActions(op, row)));
      if (op.id === "skills" || op.id === "remote") output.append(record({ enabled: reply.enabled, discovery: reply.discovery }));
    } else output.append(h("section", { class: "card" }, record(reply), reply ? rowActions(op, reply) : null));
    if (op.id === "proposal" && reply.review_complete !== true) output.append(h("p", { class: "notice warn" },
      "This proposal cannot be reviewed completely here. Installation is unavailable; review on the host CLI."));
    if (op.id === "retention_preview") output.append(button("Apply preview", () => chooseOperation("retention_apply")));
    if (op.id === "audit" && reply.next_before_id) output.append(button("Next audit page", () => {
      const previous = {};
      for (const field of workspace.querySelectorAll("[name]")) if (field.value) previous[field.name] = field.value;
      chooseOperation("audit", {}, { ...previous, before_id: reply.next_before_id });
      execute(selected, {}, { ...previous, before_id: reply.next_before_id });
    }));
    if (["scan_start", "scan", "scan_cancel"].includes(op.id) && validTarget(reply?.id)) {
      output.append(button("Refresh scan", () => { chooseOperation("scan", { scan_id: reply.id }); execute(selected, { scan_id: reply.id }, {}); }),
        button("Cancel scan", () => chooseOperation("scan_cancel", { scan_id: reply.id })));
      for (const candidate of reply.candidates || []) {
        if (candidate.promotion_available !== true || !validTarget(candidate.id) || !validTarget(candidate.suggested_slug)) continue;
        output.append(button("Add folder " + candidate.suggested_slug, () =>
        chooseOperation("scan_promote", { scan_id: reply.id, candidate_id: candidate.id },
          { slug: candidate.suggested_slug, confirmed_path: candidate.path, confirmed_markers: candidate.markers })));
      }
    }
  }
  // What the Hub cannot do, as one footnote with the host commands that do it.
  function footnote(rows) {
    if (!rows?.length) return;
    const labels = rows.map((row, i) => i ? row.label.charAt(0).toLowerCase() + row.label.slice(1) : row.label);
    const named = labels.length > 1 ? labels.slice(0, -1).join(", ") + " and " + labels.at(-1) : labels[0];
    const reasons = [...new Set(rows.map(row => row.reason).filter(Boolean))];
    clear(excluded, "Not offered here: " + named + ". ", reasons.join(" "), reasons.length ? " " : null,
      "On the daemon host: ", rows.flatMap((row, i) => [i ? ", " : null, h("code", { class: "mono" }, row.cli)]), ".");
  }
  async function start() {
    try {
      const data = await api("manage/catalog"); if (stopped) return;
      catalogue = data.operations;
      for (const [group, label] of GROUPS) {
        const control = h("button", { type: "button", "aria-pressed": "false", onclick: () => chooseGroup(group) }, label);
        control.dataset.group = group; navigation.append(control);
      }
      add.disabled = false; add.hidden = !opFor("account_add");
      footnote(data.not_offered);
      chooseGroup("accounts");
    } catch (error) { if (!stopped) message(error.message, true); }
  }
  start();
  return () => { stopped = true; eraseCode(); eraseOutput(); clear(accountsView); };
}
