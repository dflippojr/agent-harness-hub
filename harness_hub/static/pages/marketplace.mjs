// Marketplace (agent-harness #520, #519 flow 4): the owner's local Hub entries, and setting an app up from one by
// arming a pre-approved slot after a consent screen. Listing grants nothing. Nothing named by an entry is fetched: the
// only image shown is an inline one the server already checked. Every daemon string goes in as a text node.

import { api } from "../lib/api.mjs";
import { clear, command, h, when } from "../lib/dom.mjs";

const POLL_MS = 5000;
const STATES = { paired: ["Paired", "ok"], not_paired: ["Not paired", "off"], pending: ["Pending", "wait"] };

function countdown(seconds) {
  if (seconds <= 0) return "Expired; refreshing";
  const minutes = Math.floor(seconds / 60), rest = Math.floor(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")} left`;
}

function armCommand(entry, origin) {
  return `harness pairing-requests arm ${entry.catalog_app_id} --scopes ${entry.scopes.map((s) => s.scope).join(" ")}` +
    (origin ? ` --origin ${origin}` : "") + (entry.elevated.length ? " --acknowledge-elevated" : "");
}

export function renderMarketplace(main) {
  let stopped = false, busy = false, timer, ticker, data = null, offset = 0, signature = "";
  const drafts = new Map();
  const notice = h("p", { class: "notice", "aria-live": "polite", hidden: true });
  const problem = h("p", { class: "notice bad", role: "alert", hidden: true });
  const panel = h("div");
  const list = h("div", { class: "grid market-list" });
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Marketplace"), button("Refresh entries", () => load(true))),
    h("p", { class: "notice warn grants-nothing" }, h("strong", {}, "Being listed here grants nothing.")),
    h("p", { class: "muted" }, "Local entries from the daemon host's entries file. None is verified. Setting one up " +
      "arms a pre-approved slot that the app you hand it to claims once; the app then gets its token from the daemon, " +
      "and this Hub never sees it."),
    command("harness hub status"), command("harness hub entries validate"), notice, problem, panel, list);

  function button(label, action, extra = {}) {
    return h("button", { type: "button", class: "btn", onclick: action, ...extra }, label);
  }
  function message(value, bad = false) {
    notice.textContent = value;
    notice.className = bad ? "notice bad" : "notice";
    notice.hidden = !value;
  }
  function now() { return Date.now() / 1000 + offset; }
  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(() => load(), POLL_MS);
  }
  async function load(force = false) {
    if (stopped || busy && !force) return;
    clearTimeout(timer);
    let failure = "";
    try {
      const result = await api("marketplace");
      if (stopped) return;
      data = result;
      offset = typeof result.now === "number" ? result.now - Date.now() / 1000 : 0;
    } catch (error) {
      if (stopped) return;
      failure = error.message + (data?.entries ? " Showing the last entries this page read." : "");
    }
    showProblems(failure);
    draw(force);
    schedule();
  }
  function showProblems(failure) {
    const lines = [];
    if (failure) lines.push(failure);
    else if (data && data.state !== "ready") {
      lines.push(data.error || "The entries could not be read.");
      lines.push(data.entries ? `Showing the last good entries, read ${when(data.fetched_at)}.` :
        "There are no last good entries to show.");
    }
    if (!failure && data?.slots_error) lines.push(data.slots_error);
    problem.textContent = lines.join(" ");
    problem.hidden = !lines.length;
  }
  function tick() {
    for (const el of main.querySelectorAll("[data-expires]")) {
      el.textContent = countdown(Number(el.dataset.expires) - now());
    }
  }
  function draw(force) {
    const next = JSON.stringify([data?.entries ?? null, data?.withheld ?? 0]);
    if (!force && next === signature) return;
    signature = next;
    // Keep the owner's typing and focus across a poll that changed the list.
    const active = document.activeElement;
    const focused = active?.dataset?.field === "match" ? [active.closest("[data-slot]")?.dataset.slot,
      active.selectionStart] : null;
    clear(list);
    const entries = data?.entries || [];
    list.append(...entries.map(card));
    if (data?.withheld) {
      list.append(h("p", { class: "notice warn span" }, `${data.withheld} ${data.withheld === 1 ? "entry was" :
        "entries were"} withheld: the daemon's answer did not match the entries contract.`));
    }
    if (!entries.length) {
      list.append(h("p", { class: "muted span" }, data?.entries ? "The entries file lists no apps. Create it on the " +
        "daemon host with harness hub entries init." : "No entries to show."));
    }
    if (focused) {
      const target = list.querySelector(`[data-slot="${CSS.escape(focused[0] || "")}"] [data-field="match"]`);
      if (target) {
        target.focus();
        if (typeof focused[1] === "number") target.setSelectionRange(focused[1], focused[1]);
      }
    }
    tick();
  }
  function facts(rows) {
    return h("dl", { class: "facts module-facts" }, rows.map(([key, value]) => [h("dt", {}, key), h("dd", {}, value)]));
  }
  function icon(entry) {
    // Only a data: raster image survives the server's allowlist, so this never loads anything from the network.
    if (entry.icon.startsWith("data:image/")) {
      return h("img", { class: "entry-icon", src: entry.icon, alt: "", width: 48, height: 48 });
    }
    return h("span", { class: "entry-icon", "aria-hidden": "true" }, (entry.name || entry.catalog_app_id).slice(0, 1));
  }
  function card(entry) {
    const [label, tone] = STATES[entry.pending_pairing ? "pending" : entry.state] || ["Unknown", "off"];
    const box = h("section", { class: "card market-entry", "data-entry": entry.catalog_app_id },
      h("div", { class: "entry-head" }, icon(entry), h("div", {},
        h("h2", {}, entry.name || entry.catalog_app_id),
        h("p", { class: "muted entry-by" }, `${entry.publisher.name || "Unknown publisher"}` +
          (entry.version ? ` · version ${entry.version}` : "")))),
      entry.summary ? h("p", {}, entry.summary) : null,
      h("p", { class: "entry-pills" }, h("span", { class: "pill off" }, "Local entry, not verified"), " ",
        h("span", { class: "pill " + tone, "data-field": "state" }, label)),
      entry.pending_pairing ? h("p", { class: "muted" }, "A pairing request for this app is in progress." +
        (entry.state === "paired" ? " It is also already paired." : "")) : null,
      facts([["Catalog app id", entry.catalog_app_id],
        ["Browser origins", entry.browser_origins.join(", ") || "None: a native app"],
        ["Paired key ids", entry.paired.join(", ") || "None"],
        ["Scopes", entry.scopes.map((s) => s.scope + (s.tier === "elevated" ? " (elevated)" : "")).join(", ")]]));
    const native = entry.unverified_origin;
    if (native.paired.length || native.pending_pairing) {
      box.append(h("div", { class: "entry-native", "data-field": "unverified" },
        h("h3", {}, "Native keys and requests (origin not verified)"),
        h("p", { class: "muted" }, "These match the catalog id only, so they do not change the entry's state."),
        facts([["Key ids", native.paired.join(", ") || "None"],
          ["Request pending", native.pending_pairing ? "Yes" : "No"]])));
    }
    for (const slot of entry.slots || []) box.append(slotView(entry, slot));
    box.append(h("div", { class: "settings-actions" }, button(`Set up ${entry.name || entry.catalog_app_id}`,
      () => consent(entry), { class: "btn primary", "data-action": "setup" })));
    return box;
  }
  function disclosureList(items) {
    return h("ul", { class: "disclosures" }, items.map((item) => h("li", { "data-scope": item.scope },
      h("p", { class: "disclosure-head" }, h("span", { class: "pill " + (item.tier === "elevated" ? "bad" : "off") },
        item.tier), " ", h("code", {}, item.scope)),
      h("p", { class: "disclosure-text" }, item.text || "The daemon supplied no description for this scope."),
      item.justification ? h("p", { class: "setting-meta" }, "Publisher's reason (not verified): " +
        item.justification) : null)));
  }
  function slotView(entry, slot) {
    const name = entry.name || entry.catalog_app_id;
    const box = h("div", { class: "market-slot", "data-slot": slot.id, "data-state": slot.state },
      h("h3", {}, `Slot ${slot.id}`),
      facts([["Bound to", slot.origin || "a native app"],
        ["Expires", h("span", { "data-expires": slot.expires_at }, countdown(slot.expires_at - now()))]]));
    if (slot.state === "armed") {
      box.append(h("p", {}, "Armed: waiting for the app to claim it."));
    } else if (slot.state === "claimed") {
      box.append(confirmation(slot, name));
    } else if (slot.state === "approved") {
      box.append(h("p", {}, "Approved: waiting for the app to fetch its own token from the daemon."));
    }
    if (slot.disclosures.length) {
      box.append(h("details", {}, h("summary", {}, "What the daemon says each scope allows"),
        disclosureList(slot.disclosures)));
    }
    box.append(h("div", { class: "settings-actions" }, button(`Withdraw slot for ${name}`,
      () => confirmWithdraw(entry, slot), { "data-action": "withdraw" })),
    command(`harness pairing-requests deny ${slot.id}`));
    return box;
  }
  function confirmation(slot, name) {
    // The Hub never shows the code (owner decision, 2026-10-11): the owner types what the app is showing right now.
    const input = h("input", { type: "text", inputmode: "numeric", autocomplete: "off", spellcheck: "false",
      maxlength: "32", "data-field": "match", "aria-describedby": `match-help-${slot.id}` });
    input.value = drafts.get(slot.id) || "";
    const submit = button(`Confirm ${name}`, () => {
      if (submit.disabled) return;
      const match = input.value.trim();
      perform(async () => {
        await api(`pairing/${slot.id}/confirm`, { method: "POST", body: { match } });
        drafts.delete(slot.id);
        message(`Confirmed ${name}. The app now fetches its own token from the daemon.`);
      });
    }, { class: "btn primary", "data-action": "confirm" });
    const update = () => {
      drafts.set(slot.id, input.value);
      submit.disabled = busy || !input.value.trim();
    };
    input.addEventListener("input", update);
    submit.disabled = busy || !input.value.trim();
    return h("div", { class: "pairing-decision" },
      h("p", {}, `A native app claimed this slot. Confirm it with the code ${name} is showing right now.`),
      h("div", { class: "setting-field" }, h("label", {}, "Match code the app shows", input),
        h("p", { class: "setting-meta", id: `match-help-${slot.id}` }, "If the app shows no code or an error, " +
          "withdraw this slot. If your app reported that the slot was already claimed, withdraw this slot.")),
      h("div", { class: "settings-actions" }, submit),
      command(`harness pairing-requests confirm ${slot.id} --match <code>`));
  }
  function consent(entry) {
    if (busy) return;
    message("");
    const name = entry.name || entry.catalog_app_id;
    const origins = entry.browser_origins;
    let origin = origins.length === 1 ? origins[0] : "";
    const cli = h("div");
    const ack = entry.elevated.length ? h("input", { type: "checkbox", "data-field": "ack" }) : null;
    const approve = button("Approve", () => {
      if (approve.disabled) return;
      const body = { catalog_app_id: entry.catalog_app_id, scopes: entry.scopes.map((s) => s.scope) };
      if (origin) body.origin = origin;
      if (ack) body.acknowledge_elevated = true;
      perform(async () => {
        const armed = await api("marketplace/arm", { method: "POST", body });
        handoff(armed, origin);
      }, true);
    }, { class: "btn primary", "data-action": "approve" });
    const update = () => {
      approve.disabled = busy || Boolean(origins.length && !origin) || Boolean(ack && !ack.checked);
      clear(cli, command(armCommand(entry, origin || (origins.length ? "<origin>" : ""))));
    };
    let where;
    if (origins.length === 1) {
      where = h("p", {}, "Browser app: the slot is bound to exactly ", h("code", { "data-field": "origin" }, origin),
        ". Only a claim from that origin is accepted, so no match code is needed.");
    } else if (origins.length) {
      where = h("fieldset", { class: "origin-choice" }, h("legend", {}, "Browser origin to bind the slot to"),
        origins.map((value) => {
          const radio = h("input", { type: "radio", name: "origin", value });
          radio.addEventListener("change", () => { origin = value; update(); });
          return h("label", { class: "pairing-ack" }, radio, h("code", {}, value));
        }),
        h("p", { class: "setting-meta" }, "Only a claim from the chosen origin is accepted."));
    } else {
      where = h("p", {}, "Native app: when it claims the slot, you confirm it on this entry with the code the app shows.");
    }
    ack?.addEventListener("change", update);
    const cancel = button("Cancel", () => clear(panel));
    clear(panel, h("section", { class: "card warn market-consent", role: "region", "aria-labelledby": "consent-title" },
      h("h2", { id: "consent-title", tabindex: "-1" }, `Set up ${name}?`),
      h("p", {}, `${entry.catalog_app_id} · ${entry.publisher.name || "Unknown publisher"} · Local entry, not verified. ` +
        "Nothing is armed until you approve."),
      h("h3", {}, "Scopes it will get"), disclosureList(entry.scopes),
      h("h3", {}, "Where the app runs"), where,
      ack ? h("label", { class: "pairing-ack" }, ack, h("span", {},
        `I understand the elevated scopes above (${entry.elevated.join(", ")}) and still want to set up ${name}.`)) : null,
      h("div", { class: "settings-actions" }, approve, cancel), cli));
    update();
    panel.querySelector("h2").focus();
  }
  function handoff(armed, origin) {
    const slot = armed.request, name = armed.name || slot.name;
    clear(panel, h("section", { class: "card ok market-handoff", role: "region", "aria-labelledby": "handoff-title" },
      h("h2", { id: "handoff-title", tabindex: "-1" }, `Slot armed for ${name}`),
      h("p", { class: "lead" }, `Give this only to ${name}; it lets that app claim the slot once.`),
      h("p", { class: "muted" }, "Request id"), command(slot.id),
      h("p", { class: "muted" }, "Daemon URL"), command(armed.daemon_url),
      h("p", { class: "muted" }, "No secret exists yet. " + (origin ? `The app must claim it from ${origin}; ` +
        "the daemon then approves it at once." : "When the app claims it, type the code it shows on this entry " +
        "to confirm.") +
        " An unclaimed slot expires in ", h("span", { "data-expires": slot.expires_at },
        countdown(slot.expires_at - now())), "."),
      h("details", {}, h("summary", {}, "What the daemon says each scope allows"), disclosureList(slot.disclosures)),
      h("div", { class: "settings-actions" }, button("Done", () => clear(panel)))));
    message(`Armed a slot for ${name}.`);
    panel.querySelector("h2").focus();
  }
  function confirmWithdraw(entry, slot) {
    if (busy) return;
    const name = entry.name || entry.catalog_app_id;
    const ok = button("Confirm withdraw", () => perform(async () => {
      await api(`pairing/${slot.id}/deny`, { method: "POST", body: { confirm: true } });
      message(`Withdrew the slot for ${name}.`);
    }), { class: "btn primary" });
    clear(panel, h("section", { class: "card warn pairing-confirm", role: "alert" },
      h("h2", {}, `Withdraw the slot for ${name}?`),
      h("p", {}, `Slot ${slot.id} can no longer be claimed or redeemed. Set the app up again to pair it.`),
      h("div", { class: "settings-actions" }, ok, button("Cancel", () => clear(panel))),
      command(`harness pairing-requests deny ${slot.id}`)));
    ok.focus();
  }
  async function perform(action, keepPanel = false) {
    if (busy || stopped) return;
    busy = true;
    clearTimeout(timer);
    for (const control of main.querySelectorAll(".market-list button:not(.copy), .market-list input, " +
      ".market-consent button, .pairing-confirm button")) control.disabled = true;
    if (!keepPanel) clear(panel);
    try {
      await action();
    } catch (error) {
      if (stopped) return;
      clear(panel);
      if (error.status === 409) message(error.message + " The entries were refreshed; check them again.", true);
      else if (error.status === 404) message(error.message + " The entries were refreshed.", true);
      else message(error.message, true);
    }
    if (stopped) return;
    busy = false;
    await load(true);
  }
  ticker = setInterval(tick, 1000);
  load(true);
  return () => { stopped = true; clearTimeout(timer); clearInterval(ticker); };
}
