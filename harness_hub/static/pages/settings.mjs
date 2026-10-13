import { api } from "../lib/api.mjs";
import { clear, command, h } from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

const POLL_MS = 1000;
const valueText = (value) => value === null || value === undefined ? "" :
  Array.isArray(value) ? value.join("\n") : String(value);

export function renderSettings(main) {
  let stopped = false, config, schema, busy = false, restarting = false;
  const fields = new Map();
  const poll = poller(() => pollHealth());
  const status = h("p", { "aria-live": "polite", class: "notice" });
  const health = h("p", { "aria-live": "polite", class: "muted" });
  const registry = h("div", { class: "settings-registry" });
  const controls = h("div", { class: "grid" });
  const confirmation = h("div");
  const refresh = button("Reload settings", load);
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Settings"), refresh),
    status, health, registry, confirmation, controls);

  function message(text, bad = false) {
    status.textContent = text;
    status.className = bad ? "notice bad" : "notice";
  }
  function button(label, action) {
    return h("button", { type: "button", class: "btn", onclick: action }, label);
  }
  function field(label, input, ...details) {
    return h("div", { class: "setting-field" }, h("label", {}, h("span", {}, label), input), ...details);
  }
  async function perform(action) {
    if (busy || restarting) return;
    busy = true;
    const buttons = [...main.querySelectorAll("button")];
    buttons.forEach((b) => { b.disabled = true; });
    try { await action(); } catch (error) { message(error.message, true); }
    finally {
      busy = false;
      buttons.forEach((b) => { b.disabled = restarting; });
      for (const b of main.querySelectorAll("button")) b.disabled = restarting;
    }
  }
  async function load() {
    if (stopped) return;
    try {
      const rows = await Promise.all([api("settings/schema"), api("settings/config")]);
      if (stopped) return;
      [schema, config] = rows;
      drawRegistry();
      await loadControls();
      for (const b of main.querySelectorAll("button")) b.disabled = restarting;
    } catch (error) { if (!stopped) message(error.message, true); }
  }
  function drawRegistry() {
    fields.clear();
    const current = new Map(config.settings.map((row) => [row.key, row]));
    const groups = new Map();
    for (const spec of schema.settings) {
      if (!groups.has(spec.category)) groups.set(spec.category, []);
      groups.get(spec.category).push(spec);
    }
    const summary = h("section", { class: "card" }, h("h2", {}, "Settings registry"),
      h("p", {}, "Revision " + config.revision + (config.pending_revision !== null ?
        "; pending revision " + config.pending_revision : "") + (config.restart_required ? "; restart needed" : "")),
      config.warning ? h("p", { class: "notice bad" }, config.warning) : null,
      h("p", { class: "muted" }, "Edit fields, then validate or save. Reset returns a field to its inherited value."),
      command("harness config schema"), command("harness config show"),
      h("div", { class: "settings-actions" }, button("Validate", () => perform(() => submit(false))),
        button("Save", () => perform(() => submit(true))),
        button("Roll back", () => confirm("rollback")), button("Restart", () => confirm("restart"))),
      command("harness config validate <key>=<value>"), command("harness config set <key>=<value>"),
      command("harness config rollback --confirm"), command("harness config restart --confirm"),
      !config.supervised_restart ? h("p", { class: "muted" },
        "This daemon has no supervisor. Restart will report the daemon's manual restart instructions.") : null);
    clear(registry, summary);
    for (const [name, specs] of groups) {
      const group = h("section", { class: "card" }, h("h2", {}, name || "Other settings"));
      for (const spec of specs) {
        const row = current.get(spec.key) || {};
        const initial = row.pending !== null && row.pending !== undefined ? row.pending : row.effective;
        let input;
        if (spec.type === "bool" || spec.type === "enum") {
          const options = spec.type === "bool" ? ["true", "false"] : [...(spec.enum || [])];
          if (!options.includes(valueText(initial))) options.unshift(valueText(initial));
          input = h("select", {}, options.map((v) => h("option", { value: v }, v || "(empty)")));
        } else if (spec.type.endsWith("_list")) input = h("textarea", { rows: 3 });
        else input = h("input", { type: spec.type === "int" || spec.type === "float" ? "number" : "text",
          step: spec.type === "int" ? "1" : "any", min: spec.minimum, max: spec.maximum });
        input.value = valueText(initial);
        input.disabled = !spec.writable;
        const error = h("p", { class: "field-error", id: "error-" + fields.size, "aria-live": "polite" });
        input.setAttribute("aria-describedby", error.id);
        const reset = h("input", { type: "checkbox", disabled: !spec.writable });
        reset.addEventListener("change", () => { input.disabled = reset.checked || !spec.writable; });
        fields.set(spec.key, { spec, initial, input, reset, error });
        group.append(field(spec.label || spec.key, input,
          h("p", { class: "muted" }, spec.help),
          h("p", { class: "setting-meta" }, spec.key + " · " + spec.type + " · " +
            (spec.apply === "daemon_restart" ? "Restart needed" : spec.apply) + " · Default: " +
            (spec.default === null ? "unset / hidden" : JSON.stringify(spec.default))),
          spec.enum ? h("p", { class: "setting-meta" }, "Allowed: " + spec.enum.join(", ")) : null,
          spec.sensitivity === "secret_ref" ? h("p", { class: "muted" }, "Token file path only.") : null,
          !spec.writable ? h("p", { class: "muted" }, spec.guidance || "Read-only / unavailable on this daemon.") :
            h("label", { class: "reset-setting" }, reset, "Reset to inherited value"), error));
      }
      registry.append(group);
    }
  }
  function changes() {
    const out = {};
    let invalid = false;
    for (const [key, f] of fields) {
      f.error.textContent = "";
      f.input.removeAttribute("aria-invalid");
      if (!f.spec.writable) continue;
      if (f.reset.checked) { out[key] = null; continue; }
      if (f.input.value === valueText(f.initial)) continue;
      let value = f.input.value;
      if (f.spec.type === "bool") value = value === "true";
      else if (f.spec.type === "int" || f.spec.type === "float") {
        value = value.trim() === "" ? NaN : Number(value);
        if (!Number.isFinite(value) || f.spec.type === "int" && !Number.isInteger(value)) {
          f.error.textContent = "Enter a valid " + f.spec.type + ".";
          f.input.setAttribute("aria-invalid", "true");
          invalid = true;
        }
      } else if (f.spec.type.endsWith("_list")) value = value ? value.split("\n") : [];
      out[key] = value;
    }
    if (invalid) throw new Error("Correct the highlighted fields.");
    return out;
  }
  async function submit(save) {
    const payload = { revision: config.revision, changes: changes() };
    try {
      const answer = await api(save ? "settings/config" : "settings/validate",
        { method: save ? "PATCH" : "POST", body: payload });
      if (stopped) return;
      message((save ? "Saved. " : "Validated. ") + "Revision " + answer.revision +
        "; target revision " + answer.target_revision + (answer.restart_required ? ". Restart needed." : "."));
      status.append(h("ul", {}, answer.changes.map((change) => h("li", {}, change.key + ": " +
        JSON.stringify(change.from) + " → " + JSON.stringify(change.to) + " (" + change.apply + ")"))));
      if (save) await load();
    } catch (error) { await showError(error); }
  }
  async function showError(error) {
    if (stopped) return;
    message(error.message, true);
    for (const [key, row] of Object.entries(error.body?.error?.keys || {})) {
      const f = fields.get(key);
      if (f) { f.error.textContent = row.message; f.input.setAttribute("aria-invalid", "true"); }
      else status.append(h("p", {}, key + ": " + row.message));
    }
    if (error.body?.error?.code === "revision_conflict") {
      await load();
      message(error.message + " Reloaded the current revision; review your edits before saving again.", true);
    }
    if (error.body?.error?.details?.manual) status.append(h("p", {}, error.body.error.details.manual));
  }
  function confirm(action) {
    if (busy || restarting || !config) return;
    const revision = config.revision;
    const ok = button("Confirm " + action, () => perform(async () => {
      clear(confirmation);
      try {
        const answer = await api("settings/" + action, { method: "POST", body: { revision, confirm: true } });
        if (stopped) return;
        if (action === "restart" && answer.accepted) {
          restarting = true;
          message("Daemon restarting. Waiting for health and confirmed settings revision " + answer.target_revision + ".");
          poll.schedule(POLL_MS, { whileHidden: true });
        } else { message("Rolled back to revision " + answer.revision + (answer.restart_required ? ". Restart needed." : ".")); await load(); }
      } catch (error) { await showError(error); }
    }));
    clear(confirmation, h("section", { class: "card warn", role: "alert" },
      h("h2", {}, action === "rollback" ? "Roll back settings?" : "Restart the daemon?"),
      h("p", {}, action === "rollback" ? "Restore the previous confirmed settings. Unsaved edits will be discarded." :
        "The daemon will temporarily stop while applying its settings."),
      h("div", { class: "settings-actions" }, ok, button("Cancel", () => clear(confirmation)))));
    ok.focus();
  }
  async function loadControls() {
    const cards = [
      ["smart-approvals", "Smart approvals", "harness smart-approvals show", (data, box) => {
        const mode = h("select", {}, ["off", "shadow", "auto"].map((v) => h("option", { value: v }, v)));
        mode.value = data.mode;
        box.append(field("Mode", mode), h("p", { class: "setting-meta" }, data.secret_ref ? "Token file path: " + data.secret_ref : "No token file path configured."),
          command("harness smart-approvals set <mode>"),
          button("Save approvals mode", () => perform(() => saveControl("smart-approvals", { mode: mode.value }))));
      }],
      ["backends", "Backend defaults", "harness backends list", (rows, box) => {
        for (const row of rows) {
          const model = h("input", { type: "text" }); model.value = row.model;
          const effort = h("input", { type: "text" }); effort.value = row.effort;
          box.append(h("h3", {}, row.name), field("Default model", model), field("Default effort", effort),
            h("p", { class: "muted" }, "Suggested models: " + row.popular_models.map((m) => m.id).join(", ")),
            command("harness backends set " + row.name + " --model <model> --effort <effort>"),
            button("Save " + row.name, () => perform(() => saveControl("backend", { name: row.name, model: model.value, effort: effort.value }))));
        }
      }],
      ["memory", "Memory agent profile", "harness memory show", (data, box) => {
        if (data.installed === false) {
          box.append(h("p", { class: "muted" }, "The memory library module (memory_library) is not installed or is " +
            "switched off on this daemon, so there is no agent profile to edit. Turn on memory_library.enabled, or " +
            "add the module with the daemon installer's --enable-modules option."),
          command("harness config set memory_library.enabled=true"),
          command("bash install/install.sh --enable-modules memory_library"));
          return;
        }
        const profile = h("textarea", { rows: 8, disabled: !data.enabled }); profile.value = data.profile;
        box.append(field("Agent profile", profile), h("p", { class: "muted" },
          data.enabled ? "Saving commits and pushes this profile to the memory library." : "Memory library is disabled."),
          h("p", { class: "muted" }, data.refresh_error || ""),
          command("harness memory set-profile <content>"));
        if (data.enabled) box.append(button("Save profile", () => perform(() => saveControl("memory", { content: profile.value, summary: "Update agent profile from Hub Settings" }))));
      }],
      ["github", "Member GitHub access", "harness github-member-auth show", (data, box) => {
        const enabled = h("select", {}, h("option", { value: "true" }, "Allowed"), h("option", { value: "false" }, "Stopped"));
        enabled.value = String(data.enabled);
        box.append(field("Members may connect GitHub", enabled), command("harness github-member-auth set <enabled>"),
          button("Save GitHub access", () => perform(() => saveControl("github", { enabled: enabled.value === "true" }))));
        preflight(data, box);
      }],
      ["google", "Google sign-in", "harness google-signin status", (data, box) => {
        box.append(h("p", {}, "Enabled: " + data.enabled + "; ready: " + data.ready + ". Read-only.")); preflight(data, box);
      }],
    ];
    clear(controls);
    await Promise.all(cards.map(async ([path, title, cli, draw]) => {
      const box = h("section", { class: "card" }, h("h2", {}, title), command(cli));
      controls.append(box);
      try {
        const data = await api("settings/" + path);
        if (!stopped) draw(data, box);
      } catch (error) { if (!stopped) box.append(h("p", { class: "notice bad", role: "alert" }, error.message)); }
    }));
    if (stopped) return;
    controls.append(h("section", { class: "card" }, h("h2", {}, "Notifications"), command("harness notify test"),
      button("Send test notification", () => perform(async () => {
        const answer = await api("settings/notify", { method: "POST", body: {} });
        if (!stopped) message(answer.sent ? "Test notification sent." : "The daemon did not report a sent notification.");
      }))));
  }
  function preflight(data, box) {
    if (data.preflight) box.append(h("p", { class: "muted" }, "Preflight: " + data.preflight.ok +
      (data.preflight.problems.length ? ". " + data.preflight.problems.join("; ") : "")));
  }
  async function saveControl(path, body) {
    try {
      const answer = await api("settings/" + path, { method: "PUT", body });
      if (stopped) return;
      message("Saved " + path + (answer.last_commit?.head ? "; commit " + answer.last_commit.head : "") + ".");
      await load();
    } catch (error) { await showError(error); }
  }
  async function pollHealth() {
    if (stopped) return;
    try {
      const data = await api("settings/health");
      if (stopped) return;
      const wasRestarting = restarting;
      restarting = data.restarting;
      health.textContent = restarting ? "Daemon restarting…" : data.reachable ?
        "Daemon healthy; revision " + data.health.config_revision : "Daemon did not answer.";
      if (data.health?.recovery) health.textContent += " Recovery: " + JSON.stringify(data.health.recovery);
      if (wasRestarting && !restarting) {
        message(data.reachable ? "Daemon is back. Settings reloaded." : "Restart has not completed. Check the daemon before repeating any action.", !data.reachable);
        await load();
        for (const b of main.querySelectorAll("button")) b.disabled = false;
      }
    } catch (error) { if (!stopped) health.textContent = error.message; }
    // A restart keeps its 1 s poll in a hidden tab; the idle health poll pauses there.
    if (!stopped) poll.schedule(restarting ? POLL_MS : 5000, { whileHidden: restarting });
  }
  load(); pollHealth();
  return () => { stopped = true; poll.stop(); };
}
