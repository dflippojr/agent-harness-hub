import { api } from "../lib/api.mjs";
import { clear, command, h, when } from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

// Registry relationships handle module names that differ from setting names.
export function switchFor(name, schema) {
  const matches = (schema?.settings || []).filter((row) => row.type === "bool" &&
    /^[^.]+\.enabled$/.test(row.key) && row.modules?.includes(name));
  return matches.length === 1 ? matches[0] : null;
}

export function renderModules(main) {
  let stopped = false, busy = false, inventory, schema, config, outcome = null;
  const poll = poller(() => load());
  const notice = h("p", { class: "notice", "aria-live": "polite" });
  const inventoryError = h("p", { class: "notice bad", role: "alert" });
  const summary = h("section", { class: "card" });
  const list = h("div", { class: "grid" });
  const reload = button("Refresh modules", () => load());
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Modules"), reload),
    command("harness hub status"), notice, inventoryError, summary, list);

  function button(label, action) {
    return h("button", { type: "button", class: "btn", onclick: action }, label);
  }
  // The notice holds the last save's outcome, if any, plus the current read failure. A later load that reads both
  // settings documents drops the failure but keeps the outcome.
  function message(text, bad = false) {
    outcome = text ? { text, bad } : null;
    showNotice("");
  }
  function showNotice(failure) {
    notice.textContent = [outcome?.text, failure].filter(Boolean).join(" ");
    notice.className = outcome?.bad || failure ? "notice bad" : "notice";
  }
  function restartLink() {
    return h("p", {}, h("a", { href: "#/settings", class: "btn restart-link" }, "Restart in Settings"),
      " Review the restart confirmation there.");
  }
  async function load(afterWrite = false) {
    if (stopped || busy && !afterWrite) return;
    poll.cancel();
    if (!afterWrite) busy = true;
    reload.disabled = true;
    const results = await Promise.allSettled([api("modules"), api("settings/schema"), api("settings/config")]);
    if (stopped) return;
    if (results[0].status === "fulfilled") inventory = results[0].value;
    else {
      inventory = null;
      inventoryError.textContent = results[0].reason.message;
    }
    schema = results[1].status === "fulfilled" ? results[1].value : null;
    config = results[2].status === "fulfilled" ? results[2].value : null;
    showNotice(schema && config ? "" : "Switches unavailable: " +
      results.slice(1).filter((r) => r.status === "rejected").map((r) => r.reason.message).join("; "));
    if (inventory) inventoryError.textContent = inventory.error;
    busy = false;
    draw();
    for (const b of main.querySelectorAll("button")) b.disabled = false;
    poll.schedule(30000);
  }
  function draw() {
    clear(summary, h("h2", {}, "Module switches"), h("p", {}, config ? "Settings revision " + config.revision :
      "Settings could not be loaded."),
    config?.restart_required ? h("p", { class: "notice" }, "Restart needed to apply saved switches.") : null,
    config?.restart_required ? restartLink() : null,
    config?.restart_required ? command("harness config restart --confirm") : null,
    inventory?.stale ? h("p", { class: "notice" }, "Last good inventory from " + when(inventory.fetched_at) +
      ". These running states may be out of date.") : null);
    clear(list);
    const rows = [...(inventory?.modules || [])];
    if (inventory?.state === "disabled" && !rows.some((row) => row.name === "hub")) {
      rows.unshift({ name: "hub", state: "switched_off" });
    }
    if (!rows.length) list.append(h("p", { class: "muted" }, "No inventory snapshot is available."));
    for (const row of rows) {
      const spec = switchFor(row.name, schema);
      const setting = config?.settings.find((item) => item.key === spec?.key);
      const pending = typeof setting?.pending === "boolean";
      const value = pending ? setting.pending : setting?.effective;
      const failed = row.status?.state === "error";
      const box = h("section", { class: "card" + (failed ? " bad" : "") }, h("h2", {}, row.name),
        h("p", {}, "Running state: ", h("span", { class: "pill " +
          (row.state === "present" ? "ok" : "off") }, row.state)),
        typeof value === "boolean" ? h("p", {}, "Switch: " + (value ? "on" : "off") +
          (pending ? " (saved; restart needed)" : "")) : null);
      if (failed) box.append(h("p", { class: "notice bad", role: "alert" }, "Module status failed."));
      if (row.status) box.append(h("dl", { class: "facts module-facts" }, Object.entries(row.status).map(([key, val]) =>
        [h("dt", {}, key), h("dd", {}, String(val))])));
      if (row.state === "absent" || inventory.state === "absent") {
        box.append(h("p", { class: "muted" }, "Not installed. Add the package with the daemon installer's --enable-modules option."),
          command("bash install/install.sh --enable-modules " + row.name));
      } else if (spec?.writable && spec.available && setting && typeof value === "boolean") {
        const target = !value;
        const toggle = button("Switch " + row.name + (target ? " on" : " off"), () => save(spec, target));
        toggle.disabled = busy;
        box.append(toggle, command("harness config set " + spec.key + "=" + target));
        if (spec.apply === "daemon_restart") box.append(h("p", { class: "muted" }, "Changing this switch needs a daemon restart."));
      } else box.append(h("p", { class: "muted" }, spec?.guidance || "No writable module switch is available in the settings registry."));
      list.append(box);
    }
    if (inventory?.state === "absent") summary.append(h("p", {},
      "Add the Hub inventory package with the daemon installer's --enable-modules option."),
    command("bash install/install.sh --enable-modules hub"));
  }
  async function save(spec, enabled) {
    if (stopped || busy || !config) return;
    busy = true;
    poll.cancel();
    for (const b of main.querySelectorAll("button")) b.disabled = true;
    try {
      const answer = await api("settings/config", { method: "PATCH",
        body: { revision: config.revision, changes: { [spec.key]: enabled } } });
      if (stopped) return;
      message("Saved " + spec.key + "=" + enabled + ". Revision " + answer.revision +
        (answer.restart_required ? ". Restart needed; use Restart in Settings below." : "."));
    } catch (error) {
      if (stopped) return;
      const details = Object.values(error.body?.error?.keys || {}).map((row) => row.message).join("; ");
      message(error.message + (details ? " " + details : "") +
        (error.body?.error?.code === "revision_conflict" ? " Reloaded the current revision; review the switch before trying again." :
          " Check the refreshed state before trying again."), true);
    }
    await load(true);
  }
  load();
  return () => { stopped = true; poll.stop(); };
}
