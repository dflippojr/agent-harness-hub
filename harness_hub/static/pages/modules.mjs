// Modules (Hub design 9): one table of the installed modules (name, a readable status pill, a real switch, whether a
// change applies live or after a restart, and the status details in plain words), then every module that is not
// installed folded into one closed row with the installer command. A saved switch that waits for a restart reads
// "On after restart" in its row; the shell's banner slot carries Restart in Settings. Every daemon string goes in as a
// text node.

import { api } from "../lib/api.mjs";
import { blocked, blockedKey, blockedState, clear, command, h, icon, pill, placeNotice, sectionHead, sentence, table, toggle, when }
  from "../lib/dom.mjs";
import { poller } from "../lib/poll.mjs";

// When a saved switch takes effect, as a pill (the same words as Settings).
const APPLY = { live: ["Live", "ok"], daemon_restart: ["Restart", "warn"] };
const capital = (text) => text ? text[0].toUpperCase() + text.slice(1) : "";

// Registry relationships handle module names that differ from setting names.
export function switchFor(name, schema) {
  const matches = (schema?.settings || []).filter((row) => row.type === "bool" &&
    /^[^.]+\.enabled$/.test(row.key) && row.modules?.includes(name));
  return matches.length === 1 ? matches[0] : null;
}

// A module's state as the owner reads it: present is Running, switched_off is Off, and a failed status check is Error.
export function moduleStatus(row) {
  if (row.state === "present") return row.status?.state === "error" ? ["Error", "bad"] : ["Running", "ok"];
  if (row.state === "switched_off") return ["Off", "muted"];
  return [capital(String(row.state ?? "unknown").replaceAll("_", " ")), undefined];
}

// Scalar status fields in plain words: "online_count: 0" reads "Online count 0", true and false read Yes and No.
export function statusWords(status) {
  return Object.entries(status || {}).filter(([key]) => !(key === "state" && status.state === "error"))
    .map(([key, value]) => {
      const shown = value === true ? "Yes" : value === false ? "No" : value === null || value === "" ? "none" : String(value);
      return `${capital(key.replaceAll("_", " ").replaceAll(".", " "))} ${shown}`;
    }).join(" · ");
}

export function renderModules(main) {
  let stopped = false, busy = false, inventory, schema, config, drawn = "", outcome = null;
  let readError = null; // why the last inventory read failed, or null once one succeeds
  const poll = poller(() => load());
  const revision = h("span", { class: "muted modules-revision" });
  const notice = h("p", { class: "notice", "aria-live": "polite", "data-field": "notice" });
  const inventoryError = h("p", { class: "notice bad", role: "alert", hidden: true });
  const commands = h("div");
  const body = h("div");
  const reload = h("button", { type: "button", class: "btn ghost", onclick: () => load() }, icon("refresh"),
    "Refresh modules");
  clear(main, h("header", { class: "page-head modules-head" }, h("h1", {}, "Modules"), revision, reload),
    commands, notice, inventoryError, body);

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
  async function load(afterWrite = false) {
    if (stopped || busy && !afterWrite) return;
    poll.cancel();
    if (!afterWrite) busy = true;
    reload.disabled = true;
    const results = await Promise.allSettled([api("modules"), api("settings/schema"), api("settings/config")]);
    if (stopped) return;
    let error = "";
    readError = results[0].status === "rejected" ? results[0].reason : null;
    if (results[0].status === "fulfilled") inventory = results[0].value;
    else {
      inventory = null;
      // A read that could not run at all shows as its state in place of the table, and says it for the switches too.
      error = blocked(readError) ? "" : readError.message;
    }
    schema = results[1].status === "fulfilled" ? results[1].value : null;
    config = results[2].status === "fulfilled" ? results[2].value : null;
    // Each refusal is a sentence; the same one from both settings reads is said once.
    showNotice(schema && config || blocked(readError) ? "" : "Switches unavailable: " + [...new Set(results.slice(1)
      .filter((r) => r.status === "rejected").map((r) => r.reason.message))].join(" "));
    if (inventory) error = sentence(inventory.error);
    inventoryError.textContent = error;
    inventoryError.hidden = !error;
    busy = false;
    reload.disabled = false;
    draw();
    poll.schedule(30000);
  }
  // Each installed module with what its row shows; absent ones go to the not-installed row.
  function rows() {
    const list = [...(inventory?.modules || [])];
    if (inventory?.state === "disabled" && !list.some((row) => row.name === "hub")) {
      list.unshift({ name: "hub", state: "switched_off" });
    }
    return list.map((row) => {
      const spec = switchFor(row.name, schema);
      const setting = config?.settings.find((item) => item.key === spec?.key);
      const pending = typeof setting?.pending === "boolean";
      const value = pending ? setting.pending : setting?.effective;
      const writable = Boolean(spec?.writable && spec.available && setting && typeof value === "boolean");
      return { row, spec, value, pending, writable };
    });
  }
  function draw() {
    const all = rows();
    const installed = all.filter(({ row }) => row.state !== "absent");
    const absent = all.filter(({ row }) => row.state === "absent").map(({ row }) => row.name);
    // A poll that finds nothing new leaves the page alone, so a focused switch keeps its focus.
    // Each read stamps a fresh fetched_at, which the page shows only for a stale snapshot.
    const shown = inventory && { ...inventory, fetched_at: inventory.stale ? inventory.fetched_at : null };
    // busy is part of it: a draw made while a refresh runs has its switches disabled, and the next draw replaces it.
    const key = JSON.stringify([shown, config?.revision, installed.map(({ spec, value, pending, writable }) =>
      [spec?.key, spec?.apply, spec?.guidance, spec?.label, value, pending, writable]), busy,
    // Which blocked state, if any, stands in for the table: Hub down and not claimed draw differently.
    blockedKey(readError)]);
    if (key === drawn) return;
    drawn = key;
    revision.textContent = config ? "Settings revision " + config.revision : "";
    clear(commands, command(["harness hub status", ...installed.filter((m) => m.writable)
      .map((m) => `harness config set ${m.spec.key}=${!m.value}`)], { parity: true, key: "modules" }));
    clear(body,
      inventory?.stale ? h("p", { class: "notice" }, "Last good inventory from " + when(inventory.fetched_at) +
        ". These running states may be out of date.") : null,
      inventory?.state === "absent" ? h("section", { class: "card" },
        h("h2", {}, "The Hub inventory module is not installed"),
        h("p", {}, "Add the Hub inventory package on the daemon host with the daemon installer's --enable-modules option, then restart the daemon."),
        command("bash install/install.sh --enable-modules hub")) : null,
      installed.length ? [sectionHead("Installed", installed.length, { id: "modules-installed" }),
        h("div", { class: "card modules-table" }, table("Installed modules",
          ["Module", "Status", "Switch", "Applies", "Details"], installed.map(tableRow)))] : null,
      !all.length && inventory?.state !== "absent" ? blockedState(readError, "Modules", () => load()) ??
        h("p", { class: "muted" }, "No inventory snapshot is available.") : null,
      absent.length ? notInstalled(absent) : null);
    // A save's outcome shows inside the table whose switch was moved (decision 5 of #65).
    placeNotice(notice, outcome ? body.querySelector(".modules-table") : null, inventoryError);
  }
  function tableRow({ row, spec, value, pending, writable }) {
    const [label, tone] = moduleStatus(row);
    // A switch the Hub cannot change has nothing to apply.
    const apply = writable ? APPLY[spec.apply] : null;
    let control;
    if (writable) {
      control = toggle(row.name, { checked: value, disabled: busy, hideLabel: true, onchange: (on) => save(spec, on) });
    } else control = h("span", { class: "muted" }, spec ? "Locked" : "No switch");
    const words = statusWords(row.status);
    const details = [
      pending ? pill(value ? "On after restart" : "Off after restart", "run") : null,
      row.status?.state === "error" ? h("span", { class: "modules-error" }, "Status check failed") : null,
      spec?.label && spec.label !== spec.key ? h("span", {}, spec.label) : null,
      words ? h("span", { class: "muted" }, words) : null,
      spec && !writable ? h("span", { class: "muted" },
        sentence(spec.guidance) || "The daemon does not let the Hub change this switch.") : null,
    ].filter(Boolean);
    return h("tr", { "data-module": row.name },
      h("td", { class: "modules-name" }, row.name),
      h("td", { class: "modules-status" }, pill(label, tone)),
      h("td", { class: "modules-switch" }, control),
      h("td", { class: "modules-applies" }, apply ? pill(...apply) : h("span", { class: "muted" }, "—")),
      h("td", { class: "modules-details" }, details.length ? details : h("span", { class: "muted" }, "—")));
  }
  function notInstalled(names) {
    return [sectionHead("Not installed", names.length, { id: "modules-absent" }),
      h("details", { class: "card troubleshoot modules-absent" },
        h("summary", {}, icon("chevron", "icon chevron"), h("span", { class: "modules-absent-names" }, names.join(", ")),
          h("span", { class: "muted modules-absent-hint" }, "Add with the daemon installer")),
        h("div", { class: "troubleshoot-body" },
          h("p", {}, "Add one on the daemon host with the daemon installer, naming it (or a comma-separated list) " +
            "in place of <name>, then restart the daemon."),
          command("bash install/install.sh --enable-modules <name>")))];
  }
  async function save(spec, enabled) {
    if (stopped) return;
    if (busy || !config) {
      // A refresh or another save is under way: redraw from the saved values, so the switch the owner moved goes back
      // instead of showing a change nobody saved.
      drawn = "";
      draw();
      return;
    }
    busy = true;
    poll.cancel();
    for (const input of main.querySelectorAll("button, input[role=switch]")) input.disabled = true;
    try {
      const answer = await api("settings/config", { method: "PATCH",
        body: { revision: config.revision, changes: { [spec.key]: enabled } } });
      if (stopped) return;
      message("Saved " + spec.key + "=" + enabled + ". Revision " + answer.revision +
        (answer.restart_required ? ". Restart needed: use Restart in Settings." : "."));
    } catch (error) {
      if (stopped) return;
      const details = Object.values(error.body?.error?.keys || {}).map((row) => row.message).join("; ");
      message(error.message + (details ? " " + details : "") +
        (error.body?.error?.code === "revision_conflict" ? " Reloaded the current revision; review the switch before trying again." :
          " Check the refreshed state before trying again."), true);
    }
    drawn = "";
    await load(true);
  }
  void load();
  return () => { stopped = true; poll.stop(); };
}
