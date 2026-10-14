import { api } from "../lib/api.mjs";
import { clear, command, h } from "../lib/dom.mjs";

const GROUPS = [["accounts", "Accounts"], ["credentials", "Provider assignments"], ["skills", "Skills"],
  ["maintenance", "Maintenance"], ["runners", "Runners and projects"], ["audit", "Audit"]];
const DEFAULT = { accounts: "accounts", credentials: "credentials", skills: "skills", maintenance: "maintenance", runners: "runners", audit: "audit" };
const LABELS = { login: "Exact Tailscale login", display_name: "Display name", secret_ref: "Opaque secret-file name (never a key or path)",
  enabled: "Member enabled", disk_quota_bytes: "Disk quota in bytes", max_running: "Running session cap", max_queued: "Queued session cap",
  content_hash: "Exact reviewed content hash", confirmation: "Type the phrase from the retention preview", projects: "Allowed projects (one per line; empty means no project gets it automatically)",
  models: "Allowed models (one per line)", confirmed_markers: "Checked markers (one per line)", confirmed_path: "Exact checked folder path",
  before_id: "Before audit row id", since: "Since timestamp (inclusive)", until: "Until timestamp (exclusive)", policy: "Provider policy" };
const targetLabel = name => ({ user_id: "Member id", pid: "Proposal id", slug: "Skill or folder slug", credential_id: "Assignment id",
  scan_id: "Scan id", candidate_id: "Candidate id", project: "Project or folder", name: "Runner name" })[name] || name;
const button = (label, action) => h("button", { type: "button", class: "btn", onclick: action }, label);
const validTarget = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(value) && !/(?:^|[^A-Za-z0-9])(?:ha|ho|hp)-[A-Za-z0-9_-]{4,}/.test(value);
const validHash = value => typeof value === "string" && /^[a-fA-F0-9]{64}$/.test(value);
const readable = value => String(value).replaceAll("_", " ");

export function renderAdmin(main) {
  let stopped = false, busy = false, catalogue = [], active = "accounts", selected, codeTimer, preview = null, reviewedProposal = null;
  const notice = h("p", { class: "notice", role: "status", "aria-live": "polite" });
  const navigation = h("div", { class: "settings-actions", "aria-label": "Administration areas" });
  const workspace = h("section", { class: "card admin-workspace" });
  const output = h("section", { class: "admin-output", "aria-live": "polite" });
  const excluded = h("section", { class: "card" });
  const refresh = button("Refresh area", () => chooseGroup(active));
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Accounts and maintenance"), refresh),
    h("p", { class: "muted" }, "Owner administration on the daemon host. Choose an area, inspect its state, then choose an action."),
    navigation, notice, workspace, output, excluded);

  function eraseOutput() { clearTimeout(codeTimer); clear(output); }
  function message(text, bad = false) { notice.textContent = text; notice.className = bad ? "notice bad" : "notice"; }
  function setBusy(value) {
    busy = value;
    for (const control of main.querySelectorAll("button, input, select, textarea")) control.disabled = value;
  }
  function chooseGroup(group) {
    if (busy || stopped) return;
    active = group; selected = catalogue.find(op => op.id === DEFAULT[group]);
    preview = null; reviewedProposal = null; eraseOutput(); drawForm(); execute(selected, {}, {});
    for (const control of navigation.children) control.setAttribute("aria-pressed", String(control.dataset.group === group));
  }
  function chooseOperation(id, targets = {}, values = {}) {
    if (busy || stopped) return;
    if (id !== "skill_install") reviewedProposal = null;
    eraseOutput(); selected = catalogue.find(op => op.id === id); drawForm(targets, values);
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
    const confirm = button("Confirm " + op.label.toLowerCase(), () => {
      clear(slot); execute(op, targets, { ...body, confirm: true });
    });
    clear(slot, h("section", { class: "notice warn", role: "region", "aria-label": "Confirm action" },
      h("h3", {}, "Confirm " + op.label.toLowerCase()), h("p", {}, op.confirmation),
      h("pre", { class: "admin-text" }, JSON.stringify({ ...targets, ...body, ...detail }, null, 2)),
      h("div", { class: "settings-actions" }, confirm, button("Cancel", () => clear(slot)))));
    confirm.focus();
  }
  async function execute(op, targets, body) {
    if (busy || stopped) return;
    eraseOutput(); setBusy(true); message("Reading or applying " + op.label.toLowerCase() + "…");
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
      message(op.method === "GET" ? "Current daemon state." : "Daemon accepted " + op.label.toLowerCase() + ". Read current state before another change.");
      if (op.id === "google_invite") {
        clear(output, h("section", { class: "card" }, h("h2", {}, "Google link code — shown once"),
          h("p", {}, "Give this code to the member. It disappears when you change actions or leave this page."),
          h("code", { class: "admin-text", "data-link-code": true }, reply.code),
          button("Dismiss code", eraseOutput)));
        codeTimer = setTimeout(eraseOutput, Math.max(0, Math.min(3600000, (reply.expires_at * 1000) - Date.now())));
      } else showResult(op, reply);
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
    const ids = op.id === "accounts" || ["account", "account_add", "account_save"].includes(op.id) ?
      ["account", "account_save", "github_reset", "google_invite", "google_cancel", "google_revoke", "google_unlink"] :
      op.id === "credentials" ? ["credential_revoke"] : op.id === "runners" ? ["runner_update"] :
      op.id === "proposal" ? ["skill_install", "skill_reject", "skill_reopen", "skill_review", "proposal_delete"] :
      op.id === "remote" ? ["folder_trust", ...(row.managed ? ["folder_forget"] : [])] :
      op.id === "skills" && Object.hasOwn(row, "id") ? ["proposal"] : op.id === "skills" || op.id === "skills_enabled" ?
      ["skill_enable", "skill_disable", "skill_projects", "skill_rollback", "skill_uninstall", "skill_export"] : [];
    return h("div", { class: "settings-actions" }, ids.flatMap(id => {
      const action = catalogue.find(action => action.id === id);
      const targets = Object.fromEntries(action.targets.map(name => [name, ({ pid: row.id, credential_id: row.id,
        user_id: row.user_id, slug: row.slug || row.project, project: row.project, name: row.name })[name]]));
      if (Object.values(targets).some(value => !validTarget(value)) ||
        id === "skill_install" && (row.review_complete !== true || !validHash(row.content_hash))) return [];
      return button(action.label, () => chooseOperation(id, targets, id === "skill_install" ? { content_hash: row.content_hash } :
        id === "skill_projects" ? { projects: row.projects_complete === true ? row.projects : [] } : {}));
    }));
  }
  function showResult(op, reply) {
    clear(output, h("h2", {}, op.label));
    let rows = Array.isArray(reply) ? reply : op.id === "skills" ? [...(reply.proposals || []), ...(reply.installed || [])] :
      op.id === "remote" ? reply.projects || [] : op.id === "audit" ? reply.items || [] : null;
    if (rows) {
      if (!rows.length) output.append(h("p", { class: "muted" }, "No records."));
      for (const row of rows) output.append(h("section", { class: "card admin-row" },
        h("h3", {}, row.display_name || row.title || row.name || row.project || row.action || row.id || "Record"), record(row), rowActions(op, row)));
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
  async function start() {
    try {
      const data = await api("manage/catalog"); if (stopped) return;
      catalogue = data.operations;
      for (const [group, label] of GROUPS) {
        const control = button(label, () => chooseGroup(group)); control.dataset.group = group; navigation.append(control);
      }
      clear(excluded, h("h2", {}, "Not offered"), data.not_offered.map(row => h("div", {},
        h("p", {}, row.label + ": " + row.reason), command(row.cli))));
      chooseGroup("accounts");
    } catch (error) { if (!stopped) message(error.message, true); }
  }
  start();
  return () => { stopped = true; clearTimeout(codeTimer); eraseOutput(); };
}
