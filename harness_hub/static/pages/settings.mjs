// Settings (Hub design 8): a category list with a filter beside one category at a time, instead of one long column.
// The list holds the registry's categories, then the daemon's other controls. Each setting is a row: label and help
// on the left, a right-sized input on the right, and the key, when a change applies and the default underneath; an
// unset value shows the default as its placeholder. An edited row gets a Changed pill and a Reset link, and a save bar
// (Discard, Validate, Save) sticks to the bottom of the page while there are edits. The revision, Roll back and Restart
// sit in the page head. Every daemon string goes in as a text node.

import { api } from "../lib/api.mjs";
import { blockedState, clear, command, h, pill, sentence, when } from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

const POLL_MS = 1000;
const valueText = (value) => value === null || value === undefined ? "" :
  Array.isArray(value) ? value.join("\n") : String(value);
// When a saved change takes effect, as a pill.
const APPLY = { live: ["Live", "ok"], daemon_restart: ["Restart", "warn"], installer_only: ["Installer only", "muted"] };
const RECOVERY = { lkg_restore: "restored its last good settings",
  overlay_quarantined: "set aside saved settings it could not load" };
const capital = (text) => text ? text[0].toUpperCase() + text.slice(1) : "";
const isList = (spec) => spec.type.endsWith("_list");

// A value as the owner reads it: On and Off, not true and false; a list on one line.
function shown(value, spec) {
  if (value === null || value === undefined || value === "") return "not set";
  if (spec.type === "bool") return String(value) === "true" ? "On" : "Off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
  return String(value);
}

function range(min, max) {
  const has = (n) => typeof n === "number";
  if (has(min) && has(max)) return `${min} to ${max}`;
  if (has(min)) return `${min} or more`;
  return has(max) ? `up to ${max}` : "";
}

// The caption over a setting's input: what it takes.
function caption(spec) {
  if (spec.sensitivity === "secret_ref") return "Token file path";
  const kind = { int: "Whole number", float: "Number", bool: "On or off", enum: "Choose one" }[spec.type]
    || (isList(spec) ? "One per line" : "Text");
  const limits = spec.type === "int" || spec.type === "float" ? range(spec.minimum, spec.maximum) : "";
  return limits ? `${kind}, ${limits}` : kind;
}

function recoveryText(recovery) {
  const kind = typeof recovery === "string" ? recovery : recovery?.recovery;
  if (!kind) return "";
  return "The daemon " + (RECOVERY[kind] || `recovered its settings (${String(kind).replaceAll("_", " ")})`) +
    (recovery.reason ? ": " + recovery.reason : "") + (typeof recovery.at === "number" ? ", " + when(recovery.at) : "") + ".";
}

export function renderSettings(main) {
  let stopped = false, config, schema, busy = false, restarting = false;
  let selected = null; // the open category: "registry:<name>" or "control:<id>"
  let chosen = false; // the owner picked the open category; until then the first registry category opens
  let query = ""; // the filter, lower case
  const fields = new Map(); // setting key → its row, input and draft state
  const groups = new Map(); // category id → { kind, title, keys, li, link, count, section }, in list order
  const poll = poller(() => pollHealth());
  const chip = h("span", { class: "revision-chip", role: "status", hidden: true });
  const lede = h("p", { class: "lede" });
  const notice = h("p", { "aria-live": "polite", class: "notice" });
  const health = h("p", { "aria-live": "polite", class: "muted settings-health" });
  const warning = h("div");
  const confirmation = h("div");
  const filter = h("input", { type: "search", class: "settings-filter", placeholder: "Filter settings",
    "aria-label": "Filter settings", autocomplete: "off" });
  const registryList = h("ul", { "aria-labelledby": "settings-registry-label" });
  const controlList = h("ul", { "aria-labelledby": "settings-controls-label" });
  const registryLabel = h("p", { class: "nav-group", id: "settings-registry-label" }, "Registry");
  const controlLabel = h("p", { class: "nav-group", id: "settings-controls-label" }, "Controls");
  const pane = h("div", { class: "settings-pane" });
  const noMatch = h("p");
  const empty = h("div", { class: "card empty", hidden: true }, h("h2", {}, "No settings match"), noMatch,
    button("Clear filter", () => { filter.value = ""; setQuery(""); filter.focus(); }));
  const dockStatus = h("p", { "aria-live": "polite", class: "notice" });
  const summary = h("span", { class: "savebar-text" });
  const discard = button("Discard", () => discardAll(), "btn ghost");
  const validate = button("Validate", () => perform(() => submit(false), dockMessage));
  const save = button("Save", () => perform(() => submit(true), dockMessage), "btn primary");
  const close = button("Close", () => dockMessage(""), "btn ghost");
  const dock = h("div", { class: "settings-dock", role: "region", "aria-label": "Settings changes", hidden: true },
    dockStatus, h("div", { class: "savebar" }, summary, discard, validate, save, close));
  clear(main, h("header", { class: "page-head settings-head" }, h("h1", {}, "Settings"), chip,
    button("Roll back", () => confirm("rollback"), "btn ghost"), button("Restart", () => confirm("restart"), "btn ghost")),
  lede, command(["harness config schema", "harness config show", "harness config validate <key>=<value>",
    "harness config set <key>=<value>", "harness config rollback --confirm", "harness config restart --confirm"],
  { parity: true }), notice, health, confirmation, warning,
  h("div", { class: "settings-layout" },
    h("nav", { class: "settings-cats", "aria-label": "Setting categories" }, filter,
      h("div", { class: "settings-cat-lists" }, registryLabel, registryList, controlLabel, controlList)),
    h("div", { class: "settings-main" }, pane, empty)), dock);
  filter.addEventListener("input", () => setQuery(filter.value));
  drawHead();
  const controls = controlCards().map((card) => {
    const body = h("div", { class: "settings-control-body" });
    const status = h("p", { "aria-live": "polite", class: "notice" });
    const group = listItem("control:" + card.id, "control", card.title, controlList);
    group.section = h("section", { class: "card settings-control", hidden: true, "aria-labelledby": `control-${card.id}` },
      h("div", { class: "settings-group-head" }, h("h2", { id: `control-${card.id}` }, card.title)), body, status);
    Object.assign(group, { card, body, report: (text, bad = false) => {
      status.textContent = text;
      status.className = bad ? "notice bad" : "notice";
    } });
    pane.append(group.section);
    return group;
  });
  applyView();

  function button(label, action, className = "btn") {
    return h("button", { type: "button", class: className, onclick: action }, label);
  }
  function message(text, bad = false) {
    notice.textContent = text;
    notice.className = bad ? "notice bad" : "notice";
  }
  function dockMessage(text, bad = false) {
    dockStatus.textContent = text;
    dockStatus.className = bad ? "notice bad" : "notice";
    updateDock();
  }
  function field(label, input, ...details) {
    return h("div", { class: "setting-field" }, h("label", {}, h("span", {}, label), input), ...details);
  }
  // Disable every action while one runs or the daemon restarts; the category list and the filter keep working.
  function lock(on) {
    for (const b of main.querySelectorAll("button:not(.settings-cat)")) b.disabled = on;
  }
  async function perform(action, report = message) {
    if (busy || restarting) return;
    busy = true;
    lock(true);
    try { await action(); } catch (error) { report(error.message, true); }
    finally {
      busy = false;
      lock(restarting);
    }
  }
  async function load(keepDrafts = false) {
    if (stopped) return;
    try {
      const rows = await Promise.all([api("settings/schema"), api("settings/config")]);
      if (stopped) return;
      [schema, config] = rows;
      drawRegistry(keepDrafts);
      await loadControls();
      lock(restarting);
    } catch (error) {
      if (stopped) return;
      // Before any settings were read, a read that could not run at all shows as its state, with the step that fixes it.
      const state = config ? null : blockedState(error, "Settings", () => load());
      if (state) clear(warning, state);
      message(state ? "" : error.message, !state);
    }
  }

  // The saved revision and its state, as a chip in the page head.
  function revisionState() {
    if (config.restart_required) return ["restart needed", "warn"];
    if (config.pending_revision !== null && config.pending_revision !== undefined) return [`pending ${config.pending_revision}`, "ok"];
    return config.confirmed === false ? ["not confirmed", "warn"] : ["confirmed", "ok"];
  }
  function drawHead() {
    let text = "", tone = "ok";
    if (restarting) [text, tone] = ["Restarting", "warn"];
    else if (config) {
      const [state, stateTone] = revisionState();
      [text, tone] = [`Revision ${config.revision} · ${state}`, stateTone];
    }
    chip.hidden = !text;
    chip.className = `revision-chip ${tone}`;
    chip.textContent = text;
    clear(lede, "Daemon settings, saved as revisions you can roll back.", config && !config.supervised_restart ?
      " Restart is manual on this daemon: it has no supervisor, so Restart shows the steps." : "");
    clear(warning, config?.warning ? h("p", { class: "notice bad" }, sentence(config.warning)) : null);
  }

  // A category in the list, with its count; selecting it shows its section alone.
  function listItem(id, kind, title, list) {
    const count = kind === "registry" ? h("span", { class: "count" }) : null;
    const open = () => { selected = id; chosen = true; applyView(); };
    const link = h("button", { type: "button", class: "settings-cat", onclick: open },
      h("span", { class: "settings-cat-label" }, title), count ? [" ", count] : null);
    const li = h("li", {}, link);
    list.append(li);
    const group = { kind, title, keys: [], li, link, count, section: null };
    groups.set(id, group);
    return group;
  }

  function drawRegistry(keepDrafts) {
    const drafts = new Map();
    if (keepDrafts) for (const [key, f] of fields) if (changed(f)) drafts.set(key, { value: f.input.value, resetting: f.resetting });
    fields.clear();
    for (const [id, group] of [...groups]) {
      if (group.kind !== "registry") continue;
      group.section.remove();
      groups.delete(id);
    }
    clear(registryList);
    const current = new Map(config.settings.map((row) => [row.key, row]));
    const categories = new Map();
    for (const spec of schema.settings) {
      const name = spec.category || "Other settings";
      if (!categories.has(name)) categories.set(name, []);
      categories.get(name).push(spec);
    }
    const sections = [];
    let n = 0;
    for (const [name, specs] of categories) {
      const id = "registry:" + name;
      const group = listItem(id, "registry", name, registryList);
      const restart = specs.filter((spec) => spec.apply === "daemon_restart").length;
      const note = restart ? (restart === 1 ? "1 needs a restart" : `${restart} need a restart`) :
        specs.every((spec) => spec.apply === "live") ? "All apply live" : "";
      const headingId = `settings-group-${n++}`;
      group.section = h("section", { class: "card settings-group", hidden: true, "aria-labelledby": headingId },
        h("div", { class: "settings-group-head" }, h("h2", { id: headingId }, name), note ? h("span", { class: "muted" }, note) : null));
      for (const spec of specs) {
        group.keys.push(spec.key);
        group.section.append(settingRow(spec, current.get(spec.key) || {}, id, drafts.get(spec.key)));
      }
      sections.push(group.section);
    }
    // Registry categories first, then the controls, in the list and in the pane.
    for (const [id, group] of [...groups]) {
      if (group.kind !== "control") continue;
      groups.delete(id);
      groups.set(id, group);
    }
    pane.prepend(...sections);
    if (!chosen) selected = null;
    drawHead();
    updateDock();
    applyView();
  }

  function settingRow(spec, row, category, draft) {
    const n = fields.size;
    const label = spec.label || spec.key;
    const initial = row.pending !== null && row.pending !== undefined ? row.pending : row.effective;
    const ids = { label: `setting-${n}-label`, input: `setting-${n}`, caption: `setting-${n}-caption`, error: `setting-${n}-error` };
    const choice = spec.type === "bool" || spec.type === "enum";
    let input;
    if (choice) {
      const options = spec.type === "bool" ? [["true", "On"], ["false", "Off"]] : (spec.enum || []).map((v) => [v, v || "None"]);
      if (!options.some(([v]) => v === valueText(initial))) options.unshift([valueText(initial), shown(initial, spec)]);
      input = h("select", {}, options.map(([v, text]) => h("option", { value: v }, text)));
    } else if (isList(spec)) input = h("textarea", { rows: 3 });
    else input = h("input", { type: spec.type === "int" || spec.type === "float" ? "number" : "text",
      step: spec.type === "int" ? "1" : "any", min: spec.minimum, max: spec.maximum });
    input.id = ids.input;
    input.setAttribute("aria-labelledby", ids.label);
    input.setAttribute("aria-describedby", `${ids.caption} ${ids.error}`);
    input.value = valueText(initial);
    if (!choice && valueText(initial) === "") {
      input.setAttribute("placeholder", spec.default === null || spec.default === undefined || spec.default === "" ?
        "Not set" : `${shown(spec.default, spec)} (default)`);
    }
    input.disabled = !spec.writable;
    const [applyLabel, tone] = APPLY[spec.apply] || [capital(String(spec.apply || "").replaceAll("_", " ")), "muted"];
    const changedPill = pill("Changed", "run", { hidden: true });
    const reset = h("button", { type: "button", class: "btn text setting-reset", hidden: true,
      "aria-label": `Reset ${label} to its default` }, "Reset");
    const error = h("p", { class: "field-error", id: ids.error, "aria-live": "polite" });
    const el = h("div", { class: isList(spec) || spec.type === "string" ? "setting-row wide" : "setting-row", "data-key": spec.key },
      h("div", { class: "setting-text" },
        h("h3", {}, h("span", { id: ids.label }, label), changedPill),
        spec.help ? h("p", { class: "setting-help" }, spec.help) : null,
        h("div", { class: "setting-facts" }, h("span", { class: "mono" }, spec.key), applyLabel ? pill(applyLabel, tone) : null,
          h("span", {}, spec.default === null || spec.default === undefined ? "no default" : "default " + shown(spec.default, spec)),
          row.pending !== null && row.pending !== undefined ? pill("Saved, waits for a restart", "warn") : null, reset)),
      h("div", { class: "setting-control" },
        h("label", { class: "setting-caption", for: ids.input, id: ids.caption }, caption(spec)), input,
        !spec.writable ? h("p", { class: "hint" }, sentence(spec.guidance) || "Read-only on this daemon.") : null, error));
    const f = { spec, row, initial, category, input, error, el, changedPill, reset, resetting: false, invalid: false };
    const edited = () => { f.resetting = false; markField(f); updateDock(); applyView(); };
    input.addEventListener("input", edited);
    input.addEventListener("change", edited);
    reset.addEventListener("click", () => resetField(f));
    if (draft && spec.writable) {
      input.value = draft.value;
      f.resetting = draft.resetting;
    }
    fields.set(spec.key, f);
    markField(f);
    return el;
  }

  // Edited, or reset to the inherited value (the change saves as null).
  function changed(f) {
    return f.spec.writable && (f.resetting || f.input.value !== valueText(f.initial));
  }
  // A value saved through the registry can go back to the one it inherits; an edit can always go back.
  function markField(f) {
    const edit = changed(f);
    f.el.className = (f.el.className.includes("wide") ? "setting-row wide" : "setting-row") + (edit ? " changed" : "");
    f.changedPill.hidden = !edit;
    f.reset.hidden = !f.spec.writable || f.resetting || !(edit || f.row.source === "managed");
  }
  function resetField(f) {
    const managed = f.row.source === "managed";
    f.input.value = valueText(managed ? f.row.inherited : f.initial);
    f.resetting = managed;
    setFieldError(f, "");
    markField(f);
    updateDock();
    applyView();
    f.input.focus();
  }
  function setFieldError(f, text) {
    f.error.textContent = text;
    f.invalid = Boolean(text);
    if (text) f.input.setAttribute("aria-invalid", "true");
    else f.input.removeAttribute("aria-invalid");
  }
  // Open the category of the first field with an error, so the owner sees it.
  function showFirstError() {
    const f = [...fields.values()].find((item) => item.invalid);
    if (!f) return;
    if (!matches(f.spec)) { filter.value = ""; query = ""; }
    selected = f.category;
    chosen = true;
    applyView();
  }
  function discardAll() {
    for (const f of fields.values()) {
      f.input.value = valueText(f.initial);
      f.resetting = false;
      setFieldError(f, "");
      markField(f);
    }
    dockMessage("");
    applyView();
  }

  function describe(f) {
    const to = f.resetting ? "default" : shown(isList(f.spec) ? f.input.value.split("\n").filter(Boolean) : f.input.value, f.spec);
    return `${f.spec.key} ${shown(f.initial, f.spec)} → ${to}`;
  }
  function updateDock() {
    const edits = [...fields.values()].filter(changed);
    clear(summary, edits.length ? [h("strong", {}, edits.length === 1 ? "1 unsaved change" : `${edits.length} unsaved changes`),
      h("span", { class: "mono savebar-detail" }, " · " + describe(edits[0]) +
        (edits.length > 1 ? ` and ${edits.length - 1} more` : ""))] : null);
    for (const b of [discard, validate, save]) b.hidden = !edits.length;
    close.hidden = edits.length > 0;
    dock.hidden = !edits.length && !dockStatus.textContent;
  }

  function matches(spec) {
    return !query || [spec.label, spec.key, spec.help].some((text) => String(text || "").toLowerCase().includes(query));
  }
  function setQuery(text) {
    query = text.trim().toLowerCase();
    applyView();
  }
  // Show the open category alone, its rows narrowed by the filter; the list keeps the categories with a match.
  function applyView() {
    let first = null;
    for (const [id, group] of groups) {
      let visible;
      if (group.kind === "registry") {
        const rows = group.keys.map((key) => fields.get(key));
        let hits = 0, edits = 0, invalid = 0;
        for (const f of rows) {
          f.el.hidden = !matches(f.spec);
          if (!f.el.hidden) hits++;
          if (changed(f)) edits++;
          if (f.invalid) invalid++;
        }
        visible = hits > 0;
        group.count.textContent = String(query ? hits : rows.length);
        group.count.className = invalid ? "count bad" : edits ? "count changed" : "count";
      } else visible = !query || group.title.toLowerCase().includes(query);
      group.li.hidden = !visible;
      if (visible && !first) first = id;
    }
    // A list header goes with the last of its categories.
    const listed = (kind) => [...groups.values()].some((group) => group.kind === kind && !group.li.hidden);
    registryLabel.hidden = !listed("registry");
    controlLabel.hidden = !listed("control");
    if (!groups.has(selected) || groups.get(selected).li.hidden) selected = first;
    for (const [id, group] of groups) {
      group.section.hidden = id !== selected;
      group.link.setAttribute("aria-current", String(id === selected));
    }
    noMatch.textContent = `Nothing in the registry or the controls matches “${query}”.`;
    empty.hidden = Boolean(selected);
  }

  function changes() {
    const out = {};
    let invalid = false;
    for (const [key, f] of fields) {
      setFieldError(f, "");
      if (!changed(f)) continue;
      if (f.resetting) { out[key] = null; continue; }
      let value = f.input.value;
      if (f.spec.type === "bool") value = value === "true";
      else if (f.spec.type === "int" || f.spec.type === "float") {
        value = value.trim() === "" ? NaN : Number(value);
        if (!Number.isFinite(value) || f.spec.type === "int" && !Number.isInteger(value)) {
          setFieldError(f, f.spec.type === "int" ? "Enter a whole number." : "Enter a number.");
          invalid = true;
        }
      } else if (isList(f.spec)) value = value ? value.split("\n") : [];
      out[key] = value;
    }
    if (invalid) showFirstError();
    applyView();
    if (invalid) throw new Error("Correct the highlighted fields.");
    return out;
  }
  async function submit(save) {
    dockMessage("");
    const payload = { revision: config.revision, changes: changes() };
    try {
      const answer = await api(save ? "settings/config" : "settings/validate",
        { method: save ? "PATCH" : "POST", body: payload });
      if (stopped) return;
      if (save) await load();
      if (stopped) return;
      dockMessage((save ? "Saved. " : "Validated. ") + "Revision " + answer.revision +
        "; target revision " + answer.target_revision + (answer.restart_required ? ". Restart needed." : "."));
      dockStatus.append(h("ul", {}, answer.changes.map((change) => h("li", {}, change.key + ": " +
        JSON.stringify(change.from) + " → " + JSON.stringify(change.to) + " (" + change.apply + ")"))));
    } catch (error) { await showError(error, dockMessage, dockStatus); }
  }
  async function showError(error, report = message, box = notice) {
    if (stopped) return;
    report(error.message, true);
    for (const [key, row] of Object.entries(error.body?.error?.keys || {})) {
      const f = fields.get(key);
      if (f) setFieldError(f, sentence(row.message));
      else box.append(h("p", {}, key + ": " + sentence(row.message)));
    }
    showFirstError();
    applyView();
    if (error.body?.error?.code === "revision_conflict") {
      await load(true);
      report(error.message + " Reloaded the current revision and kept your edits; review them before saving again.", true);
    }
    if (error.body?.error?.details?.manual) box.append(h("p", {}, error.body.error.details.manual));
  }
  function confirm(action) {
    if (busy || restarting || !config) return;
    const revision = config.revision;
    const ok = button("Confirm " + action, () => perform(async () => {
      clear(confirmation);
      try {
        const answer = await api("settings/" + action, { method: "POST", body: { revision, confirm: true } });
        if (stopped) return;
        if (action === "rollback") {
          message("Rolled back to revision " + answer.revision + (answer.restart_required ? ". Restart needed." : "."));
          await load();
        } else if (answer.accepted) {
          restarting = true;
          drawHead();
          message("Daemon restarting. Waiting for health and confirmed settings revision " + answer.target_revision + ".");
          poll.schedule(POLL_MS, { whileHidden: true });
        } else message("The daemon did not start a restart.", true);
      } catch (error) { await showError(error); }
    }), "btn primary");
    clear(confirmation, h("section", { class: "card warn settings-confirm", role: "alert" },
      h("h2", {}, action === "rollback" ? "Roll back settings?" : "Restart the daemon?"),
      h("p", {}, action === "rollback" ? "Restore the previous confirmed settings. Unsaved edits will be discarded." :
        config.supervised_restart ? "The daemon will temporarily stop while applying its settings." :
          "This daemon has no supervisor, so the Hub shows the steps to restart it on its host."),
      h("div", { class: "settings-actions" }, ok, button("Cancel", () => clear(confirmation)))));
    ok.focus();
  }

  // The daemon's other controls, listed after the registry's categories: each one's title, the read that fills it
  // with its host command, and how it draws. Notifications has no read.
  function controlCards() {
    return [
      { id: "smart-approvals", title: "Smart approvals mode", read: "smart-approvals", cli: "harness smart-approvals show",
        draw: (data, box, report) => {
          const mode = h("select", {}, ["off", "shadow", "auto"].map((v) => h("option", { value: v }, capital(v))));
          mode.value = data.mode;
          box.append(field("Mode", mode), h("p", { class: "hint" }, data.secret_ref ? "Token file path: " + data.secret_ref :
            "No token file path configured."),
          h("div", { class: "settings-actions" }, button("Save approvals mode",
            () => perform(() => saveControl("smart-approvals", { mode: mode.value }, report), report))),
          command("harness smart-approvals set <mode>", { parity: true }));
        } },
      { id: "backends", title: "Backend defaults", read: "backends", cli: "harness backends list",
        draw: (rows, box, report) => {
          if (!rows.length) box.append(h("p", { class: "muted" }, "This daemon reports no backends."));
          for (const row of rows) {
            const model = h("input", { type: "text" }); model.value = row.model;
            const effort = h("input", { type: "text" }); effort.value = row.effort;
            box.append(h("h3", {}, row.name), h("div", { class: "settings-pair" }, field("Default model", model),
              field("Default effort", effort)),
            row.popular_models?.length ? h("p", { class: "hint" }, "Suggested models: " +
              row.popular_models.map((m) => m.id).join(", ")) : null,
            h("div", { class: "settings-actions" }, button("Save " + row.name, () => perform(() => saveControl("backend",
              { name: row.name, model: model.value, effort: effort.value }, report), report))),
            command("harness backends set " + row.name + " --model <model> --effort <effort>", { parity: true }));
          }
        } },
      { id: "memory", title: "Memory agent profile", read: "memory", cli: "harness memory show",
        draw: (data, box, report) => {
          if (data.installed === false) {
            box.append(h("p", { class: "muted" }, "The memory library module (memory_library) is not installed or is " +
              "switched off on this daemon, so there is no agent profile to edit. Turn on memory_library.enabled, or " +
              "add the module with the daemon installer's --enable-modules option."),
            command("harness config set memory_library.enabled=true"),
            command("bash install/install.sh --enable-modules memory_library"));
            return;
          }
          const profile = h("textarea", { rows: 8, disabled: !data.enabled }); profile.value = data.profile;
          box.append(field("Agent profile", profile), h("p", { class: "hint" },
            data.enabled ? "Saving commits and pushes this profile to the memory library." : "Memory library is disabled."),
          data.refresh_error ? h("p", { class: "notice bad" }, sentence(data.refresh_error)) : null);
          if (data.enabled) box.append(h("div", { class: "settings-actions" }, button("Save profile", () => perform(() =>
            saveControl("memory", { content: profile.value, summary: "Update agent profile from Hub Settings" }, report), report))));
          box.append(command("harness memory set-profile <content>", { parity: true }));
        } },
      { id: "github", title: "Member GitHub access", read: "github", cli: "harness github-member-auth show",
        draw: (data, box, report) => {
          const enabled = h("select", {}, h("option", { value: "true" }, "Allowed"), h("option", { value: "false" }, "Stopped"));
          enabled.value = String(data.enabled);
          box.append(field("Members may connect GitHub", enabled),
            h("div", { class: "settings-actions" }, button("Save GitHub access", () => perform(() =>
              saveControl("github", { enabled: enabled.value === "true" }, report), report))),
            command("harness github-member-auth set <enabled>", { parity: true }));
          preflight(data, box);
        } },
      { id: "google", title: "Google sign-in", read: "google", cli: "harness google-signin status",
        draw: (data, box) => {
          box.append(h("div", { class: "chips" }, pill(data.enabled ? "On" : "Off", data.enabled ? "ok" : "muted"),
            pill(data.ready ? "Ready" : "Not ready", data.ready ? "ok" : "warn")),
          h("p", { class: "hint" }, "Read-only here. Google sign-in is set up on the daemon host."));
          preflight(data, box);
        } },
      { id: "notify", title: "Notifications", draw: (_, box, report) => box.append(
        h("p", { class: "muted" }, "Send a test message through the daemon's notification channel."),
        h("div", { class: "settings-actions" }, button("Send test notification", () => perform(async () => {
          const answer = await api("settings/notify", { method: "POST", body: {} });
          if (!stopped) report(answer.sent ? "Test notification sent." : "The daemon did not report a sent notification.");
        }, report))), command("harness notify test", { parity: true })) },
    ];
  }
  async function loadControls() {
    await Promise.all(controls.map(async ({ card, body, report }) => {
      report("");
      clear(body, card.read ? command(card.cli, { parity: true }) : null);
      if (!card.read) {
        card.draw(null, body, report);
        return;
      }
      try {
        const data = await api("settings/" + card.read);
        if (!stopped) card.draw(data, body, report);
      } catch (error) { if (!stopped) body.append(h("p", { class: "notice bad", role: "alert" }, error.message)); }
    }));
  }
  function preflight(data, box) {
    if (!data.preflight) return;
    const problems = data.preflight.problems || [];
    box.append(h("p", { class: data.preflight.ok ? "hint" : "notice warn" }, data.preflight.ok ? "Preflight checks passed." :
      "Preflight found problems" + (problems.length ? ": " + problems.join("; ") : ".")));
  }
  async function saveControl(path, body, report) {
    try {
      const answer = await api("settings/" + path, { method: "PUT", body });
      if (stopped) return;
      await load();
      if (stopped) return;
      report("Saved" + (answer.last_commit?.head ? "; commit " + answer.last_commit.head : "") + ".");
    } catch (error) { report(error.message, true); }
  }

  async function pollHealth() {
    if (stopped) return;
    try {
      const data = await api("settings/health");
      if (stopped) return;
      const wasRestarting = restarting;
      restarting = data.restarting;
      const state = restarting ? "Daemon restarting…" : data.reachable ? "" : data.answered ?
        "Daemon answered its health check with HTTP " + data.status + "." : "Daemon did not answer.";
      health.textContent = [state, recoveryText(data.health?.recovery)].filter(Boolean).join(" ");
      if (wasRestarting !== restarting) drawHead();
      if (wasRestarting && !restarting) {
        message(data.reachable ? "Daemon is back. Settings reloaded." : "Restart has not completed. Check the daemon before repeating any action.", !data.reachable);
        await load();
        lock(false);
      }
    } catch (error) { if (!stopped) health.textContent = error.message; }
    // A restart keeps its 1 s poll in a hidden tab; the idle health poll pauses there.
    if (!stopped) poll.schedule(restarting ? POLL_MS : 5000, { whileHidden: restarting });
  }
  void load(); void pollHealth();
  return () => { stopped = true; poll.stop(); };
}
