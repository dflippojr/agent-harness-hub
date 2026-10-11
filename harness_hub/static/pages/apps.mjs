import { api } from "../lib/api.mjs";
import { clear, command, h, when } from "../lib/dom.mjs";

const text = (value) => value === null || value === undefined ? "Unavailable" : String(value);
const revoked = (row) => row.revoked_at !== null || ["revoked", "erasure_pending"].includes(row.state);
const canRevoke = (row) => Boolean(row.id) && !row.this_hub && row.role !== "hub" && row.kind !== "web" && !revoked(row);

export function renderApps(main) {
  let stopped = false, busy = false, timer, inventory;
  const notice = h("p", { class: "notice", "aria-live": "polite" });
  const problem = h("p", { class: "notice bad", role: "alert" });
  const entries = h("div", { class: "grid apps-list" });
  const list = h("div", { class: "grid apps-list" });
  const erasures = h("section", { class: "card apps-erasures" });
  const detail = h("div");
  const confirmation = h("div");
  const refresh = button("Refresh apps", load);
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Apps"), refresh),
    command("harness hub status"), command("harness keys list"), notice, problem,
    h("h2", {}, "Installed entries"), entries, h("h2", {}, "Paired apps and keys"), list, detail, confirmation, erasures);

  function button(label, action) {
    return h("button", { type: "button", class: "btn", onclick: action }, label);
  }
  function message(value, bad = false) {
    notice.textContent = value;
    notice.className = bad ? "notice bad" : "notice";
  }
  function facts(rows) {
    return h("dl", { class: "facts module-facts" }, rows.map(([key, value]) =>
      [h("dt", {}, key), h("dd", {}, value)]));
  }
  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(load, 30000);
  }
  async function load() {
    if (stopped || busy) return;
    busy = true;
    clearTimeout(timer);
    refresh.disabled = true;
    clear(detail); clear(confirmation);
    const results = await Promise.allSettled([api("apps"), api("apps/erasures")]);
    if (stopped) return;
    inventory = results[0].status === "fulfilled" ? results[0].value : null;
    problem.textContent = inventory?.error || (results[0].status === "rejected" ? results[0].reason.message : "");
    busy = false;
    draw();
    drawErasures(results[1]);
    refresh.disabled = false;
    schedule();
  }
  function draw() {
    clear(entries); clear(list);
    const rows = inventory?.apps || [];
    for (const entry of inventory?.entries || []) {
      // The daemon's pairing ids include catalog identity AND origin checks. Names grant no association.
      const matches = rows.filter(row => entry.paired.includes(row.id));
      entries.append(h("section", { class: "card" }, h("h3", {}, entry.name || entry.catalog_app_id),
        h("p", {}, entry.catalog_app_id), h("p", {}, matches.length ?
          "Paired: " + matches.map(row => row.name).join(", ") : "No verified-origin pairing in inventory."),
        h("p", { class: "muted" }, entry.verified ? "Verified entry" : "Unsigned local entry; listing grants no permissions.")));
    }
    if (!entries.childElementCount) entries.append(h("p", { class: "muted" }, "No installed entries in inventory."));
    if (inventory?.stale) list.append(h("p", { class: "notice span" },
      "Last good inventory from " + when(inventory.fetched_at) + ". App states may be out of date; management is unavailable until refresh succeeds."));
    const order = (inventory?.entries || []).flatMap(entry => entry.paired);
    rows.sort((a, b) => (order.includes(a.id) ? order.indexOf(a.id) : order.length) -
      (order.includes(b.id) ? order.indexOf(b.id) : order.length));
    for (const row of rows) {
      const name = row.this_hub ? "This Hub" : row.name || row.id;
      const box = h("section", { class: "card" + (row.errors ? " bad" : ""), "data-app-id": row.id },
        h("h3", {}, name), h("p", {}, "Connection: ", h("span", { class: "pill " +
          (row.state === "active" ? "ok" : "off") }, row.state)),
        row.errors ? h("p", { class: "notice bad" }, "Errors in the last 24 hours") : null,
        row.token_age_warning ? h("p", { class: "notice warn" }, "Token is at least 90 days old. No automatic expiry.") : null,
        facts([["Key id", row.id], ["Kind / role", row.kind + " / " + (row.role || "app")],
          ["Scopes", row.scopes || "None"], ["Origins", row.origins.join(", ") || "None"],
          ["Catalog app", row.catalog_app_id || "None"], ["Created", when(row.created_at)],
          ["Last seen", row.last_used_at === null ? "Never used" : when(row.last_used_at)],
          ["Token age", row.token_age_seconds === null ? "Unavailable" : Math.floor(row.token_age_seconds / 86400) + " days"],
          ["Sessions", Object.entries(row.store.sessions).map(([key, value]) => key + ": " + text(value)).join(", ") || "None"],
          ["Usage requests", text(row.store.usage.requests)], ["Prompt tokens", text(row.store.usage.prompt_tokens)],
          ["Completion tokens", text(row.store.usage.completion_tokens)], ["Cost (USD)", text(row.store.usage.cost_usd)],
          ["Error count", text(row.store.errors)], ["Last error kind", row.store.last_error || "None"],
          ["Last error time", when(row.store.last_error_at)], ["Erasure scheduled", when(row.erase_after)],
          ["Erased", when(row.erased_at)]]));
      if (row.this_hub || row.role === "hub") box.append(h("p", {}, "Release this Hub on the daemon host:"),
        command("harness hub release --confirm"));
      else if (revoked(row)) box.append(h("p", {}, "Restore on the daemon host before the erasure deadline. " +
        (row.erased_at !== null ? "Data has already been erased; restore is no longer available." :
          "The host CLI returns the replacement credential to the owner.")), command("harness apps restore " + row.id));
      else if (canRevoke(row)) {
        const revoke = button("Revoke " + name, () => confirmRevoke(row));
        revoke.disabled = inventory.state !== "ready";
        box.append(revoke, command("harness keys revoke " + row.id));
      }
      if (row.kind === "app" && !revoked(row)) {
        const manage = button("Manage " + name, () => manageApp(row));
        manage.disabled = inventory.state !== "ready";
        box.append(manage, command("harness apps limits " + row.id));
      }
      list.append(box);
    }
    if (!rows.length) list.append(h("p", { class: "muted" }, "No paired app snapshot is available."));
  }
  function drawErasures(result) {
    clear(erasures, h("h2", {}, "Waiting for erasure"), command("harness apps erasures"));
    if (result.status === "rejected") {
      erasures.append(h("p", { class: "notice bad", role: "alert" }, result.reason.message)); return;
    }
    for (const row of result.value) erasures.append(h("p", {}, (row.name || row.id) + " (" + row.id +
      "): " + when(row.erase_after)), command("harness apps restore " + row.id));
    if (!result.value.length) erasures.append(h("p", { class: "muted" }, "No apps are waiting to be erased."));
  }
  function confirmAction(title, explanation, action) {
    clearTimeout(timer);
    const cancel = button("Cancel", () => { clear(confirmation); schedule(); });
    const ok = button("Confirm " + title, () => perform(action));
    clear(confirmation, h("section", { class: "card warn", role: "alert" }, h("h2", {}, title + "?"),
      h("p", {}, explanation), h("div", { class: "settings-actions" }, ok, cancel)));
    ok.focus();
  }
  function confirmRevoke(row) {
    if (busy || inventory?.state !== "ready" || !canRevoke(row)) return;
    confirmAction("revoke " + (row.name || row.id), "Revoke access for " + (row.name || row.id) +
      " (" + row.id + ") immediately. App and device data will be scheduled for erasure. Restore stays on the host CLI.",
    async () => { await api("apps/" + row.id + "/revoke", { method: "DELETE", body: { confirm: true } });
      message("Revoked " + (row.name || row.id) + ". Review the refreshed erasure deadline below."); });
  }
  async function perform(action) {
    if (busy || stopped || inventory?.state !== "ready") return;
    busy = true; clearTimeout(timer);
    for (const control of main.querySelectorAll("button,input")) control.disabled = true;
    clear(confirmation);
    try { await action(); }
    catch (error) { if (!stopped) message(error.message + " Refresh and review the state before trying again.", true); }
    if (stopped) return;
    busy = false;
    await load();
  }
  async function manageApp(row) {
    if (stopped || busy || inventory?.state !== "ready") return;
    clearTimeout(timer); busy = true;
    try {
      const data = await api("apps/" + row.id + "/limits");
      if (stopped) return;
      const running = numberField("Maximum running (1–100; blank uses default)", data.configured.max_running, 100);
      const queued = numberField("Maximum queued (1–1000; blank uses default)", data.configured.max_queued, 1000);
      const retention = numberField("Retention days (blank keeps until deleted)", data.retention_days, 36500, "any");
      clear(detail, h("section", { class: "card apps-detail" }, h("h2", {}, "Manage " + (row.name || row.id)),
        facts([["Effective caps", "Running " + text(data.effective.max_running) + "; queued " + text(data.effective.max_queued)],
          ["Counting sessions", "Running " + text(data.running) + "; queued " + text(data.queued) + "; parked " + text(data.parked)]]),
        h("p", { class: "muted" }, "Parked sessions count toward both caps."), running.field, queued.field,
        button("Save session caps", () => {
          if (!running.input.reportValidity() || !queued.input.reportValidity()) return;
          perform(async () => { await api("apps/" + row.id + "/limits", { method: "PUT", body: {
            max_running: numberValue(running.input), max_queued: numberValue(queued.input) } }); message("Session caps saved."); });
        }), command("harness apps set-limits " + row.id + " --max-running <integer> --max-queued <integer>"),
        command("harness apps set-limits " + row.id + " --set max_running=null --set max_queued=null"),
        retention.field, button("Save retention", () => {
          if (!retention.input.reportValidity()) return;
          const days = numberValue(retention.input);
          confirmAction("change retention for " + (row.name || row.id), "Apply retention " +
            (days === null ? "until deleted" : days + " days") + " to " + (row.name || row.id) +
            ". Shorter retention can erase older sessions during cleanup.", async () => {
            await api("apps/" + row.id + "/retention", { method: "PUT", body: { retention_days: days, confirm: true } });
            message("Retention saved."); });
        }), command("harness apps retention " + row.id + " --retention-days <days>"),
        command("harness apps retention " + row.id + " --set retention_days=null"),
        button("Close app details", () => { clear(detail); clear(confirmation); schedule(); })));
      detail.querySelector("h2").setAttribute("tabindex", "-1"); detail.querySelector("h2").focus();
    } catch (error) { if (!stopped) { message(error.message, true); schedule(); } }
    finally { busy = false; }
  }
  function numberField(label, value, max, step = "1") {
    const input = h("input", { type: "number", min: step === "any" ? "0.000001" : "1", max, step });
    input.value = value === null ? "" : String(value);
    return { input, field: h("div", { class: "setting-field" }, h("label", {}, label, input)) };
  }
  function numberValue(input) { return input.value.trim() === "" ? null : Number(input.value); }
  load();
  return () => { stopped = true; clearTimeout(timer); };
}
