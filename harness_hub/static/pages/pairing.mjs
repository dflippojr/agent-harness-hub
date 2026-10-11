// Pairing requests (agent-harness #519): the owner types the App code and approves, confirms or denies. The daemon
// then hands each App its own token; this page never sees one. Every daemon string goes in as a text node.

import { api } from "../lib/api.mjs";
import { clear, command, h, when } from "../lib/dom.mjs";
import { pairingRecovery } from "../lib/pairing.mjs";

const GROUPS = [
  ["pending", "Waiting for approval"], ["armed", "Armed slots"], ["claimed", "Claimed slots to confirm"],
  ["approved", "Approved, waiting for the app"], ["redeemed", "Paired"], ["denied", "Denied or withdrawn"],
  ["expired", "Expired"], ["unknown", "Other"],
];
const ACTIVE = ["pending", "armed", "claimed", "approved"];
const POLL_MS = 5000;

function nextStep(row) {
  if (row.kind === "hub") {
    return row.state === "pending" ? "Approve or deny this Hub claim on the daemon host." :
      row.state === "approved" ? "Approved: the Hub fetches its own key." : "Nothing to do.";
  }
  return {
    approve: "Type the code the app is showing right now. If the app shows no code or an error, deny.",
    claim: "Waiting for the app to claim this slot.",
    confirm: "An app claimed this slot. Redemption is waiting for confirmation. " +
      "Type the code the app is showing right now. If the app shows no code or an error, deny.",
    redeem: "Approved: waiting for the app to fetch its own token from the daemon.",
  }[row.needs] || {
    redeemed: "Paired. The app received its token directly from the daemon.",
    denied: "Denied or withdrawn. The app cannot use this request.",
    expired: "Expired before it finished. The app must ask again.",
  }[row.state] || "Nothing to do.";
}

function countdown(seconds) {
  if (seconds <= 0) return "Expired; refreshing";
  const minutes = Math.floor(seconds / 60), rest = Math.floor(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")} left`;
}

export function renderPairing(main) {
  let stopped = false, busy = false, timer, ticker, data = null, offset = 0, signature = "";
  const drafts = new Map();
  const notice = h("p", { class: "notice", "aria-live": "polite", hidden: true });
  const problem = h("p", { class: "notice bad", role: "alert", hidden: true });
  const groups = h("div", { class: "pairing-groups" });
  const confirmation = h("div");
  const refresh = button("Refresh requests", () => load(true));
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Pairing requests"), refresh),
    h("p", { class: "muted" }, "Apps asking to pair in the last day. Approving changes the request's state only: " +
      "the app then fetches its own token from the daemon, and this Hub never sees it."),
    pairingRecovery(), notice, problem, confirmation, groups);

  function button(label, action, extra = {}) {
    return h("button", { type: "button", class: "btn", onclick: action, ...extra }, label);
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
  function now() { return Date.now() / 1000 + offset; }
  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(() => load(), POLL_MS);
  }
  async function load(force = false) {
    if (stopped || busy && !force) return;
    clearTimeout(timer);
    try {
      const result = await api("pairing");
      if (stopped) return;
      data = result;
      offset = typeof result.now === "number" ? result.now - Date.now() / 1000 : 0;
      showProblem(result.confirmation_warning || "");
    } catch (error) {
      if (stopped) return;
      showProblem(error.message + (data ? " Showing the last list." : ""));
    }
    draw(force);
    schedule();
  }
  function tick() {
    for (const el of groups.querySelectorAll("[data-expires]")) {
      el.textContent = countdown(Number(el.dataset.expires) - now());
    }
  }
  function draw(force) {
    const next = JSON.stringify(data?.requests ?? null);
    if (!force && next === signature) return;
    signature = next;
    // Keep the owner's typing and focus across a poll that changed the list.
    const active = document.activeElement;
    const focused = active?.dataset?.field ? [active.closest("[data-request-id]")?.dataset.requestId,
      active.dataset.field, active.selectionStart] : null;
    clear(groups);
    const rows = data?.requests || [];
    for (const [state, label] of GROUPS) {
      const members = rows.filter((row) => (GROUPS.some(([s]) => s === row.state) ? row.state : "unknown") === state);
      if (!members.length) continue;
      groups.append(h("section", { class: "pairing-group", "data-state": state },
        h("h2", {}, `${label} (${members.length})`), h("div", { class: "grid pairing-list" }, members.map(card))));
    }
    if (!rows.length) groups.append(h("p", { class: "muted" }, data ? "No pairing requests in the last day." :
      "The pairing list is not available."));
    if (focused) {
      const target = [...groups.querySelectorAll("[data-field]")].find((el) =>
        el.dataset.field === focused[1] && el.closest("[data-request-id]")?.dataset.requestId === focused[0]);
      if (target) {
        target.focus();
        if (typeof focused[2] === "number" && target.setSelectionRange) target.setSelectionRange(focused[2], focused[2]);
      }
    }
    tick();
  }
  function facts(rows) {
    return h("dl", { class: "facts module-facts" }, rows.map(([key, value]) => [h("dt", {}, key), h("dd", {}, value)]));
  }
  function card(row) {
    const name = row.name || row.id;
    const box = h("section", { class: "card" + (row.elevated.length ? " warn" : ""), "data-request-id": row.id },
      h("h3", {}, `${name} · ${row.id}`),
      h("p", { class: "muted" }, row.kind === "hub" ? "Hub claim · name supplied by the requester" :
        "App-supplied name · only the match code on your app's screen proves this is your app"),
      h("p", {}, "State: ", h("span", { class: "pill " + (row.state === "redeemed" ? "ok" :
        ACTIVE.includes(row.state) ? "wait" : "off") }, row.state)),
      h("p", { class: "pairing-next" }, "Next step: " + nextStep(row)),
      facts([["Kind", row.kind === "hub" ? "Hub claim" : row.kind === "app" ? "App" : "Unknown"],
        ...(row.kind === "app" ? [["Started from", row.armed ? "this Hub" : "the app"]] : []),
        [row.kind === "app" && !row.armed ? "Origin (stated by the requester, not verified)" : "Origin",
          row.origin || "native app"],
        [row.kind === "app" && !row.armed ? "Catalog app (stated by the requester, not verified)" : "Catalog app",
          row.catalog_app_id || "None"],
        ["Scopes", row.scopes.join(", ") || "None"], ["Created", when(row.created_at)],
        ACTIVE.includes(row.state) && typeof row.expires_at === "number" ?
          ["Expires", h("span", { "data-expires": row.expires_at }, countdown(row.expires_at - now()))] :
          ["Finished", when(row.finished_at)],
        ...(row.key_id ? [["Key id", row.key_id]] : [])]));
    if (row.kind === "app" && row.armed) {
      box.append(h("p", { class: "muted" }, "You set the origin and catalog app id when you armed this slot."));
    }
    if (row.disclosures.length) {
      box.append(h("h4", {}, "What each scope allows"), h("ul", { class: "disclosures" }, row.disclosures.map((item) =>
        h("li", { "data-scope": item.scope },
          h("p", { class: "disclosure-head" }, h("span", { class: "pill " + (item.tier === "elevated" ? "bad" : "off") },
            item.tier), " ", h("code", {}, item.scope)),
          h("p", { class: "disclosure-text" }, item.text || "The daemon supplied no description for this scope.")))));
    }
    if (row.kind === "hub") {
      if (ACTIVE.includes(row.state)) {
        box.append(h("p", {}, "Hub claims are decided only on the daemon host. Read the code off that Hub's own screen."),
          command(`harness hub approve ${row.id} --match <code>`), command(`harness hub deny ${row.id}`));
      }
    } else if (row.kind === "app") {
      const withheld = [row.name, row.origin, row.catalog_app_id].some(value => value?.startsWith("[withheld:"));
      if (withheld && ["pending", "claimed"].includes(row.state)) {
        box.append(h("p", { class: "notice bad" },
          "Review this request with harness pairing-requests list on the host."),
          command("harness pairing-requests list"));
      } else if (row.state === "pending") box.append(decision(row, "approve"));
      else if (row.state === "claimed") box.append(decision(row, "confirm"));
      if (ACTIVE.includes(row.state)) {
        const verb = ["pending", "claimed"].includes(row.state) ? "Deny" : "Withdraw";
        box.append(h("div", { class: "settings-actions" }, button(`${verb} ${name}`, () => confirmDeny(row, verb),
          { "data-action": "deny" })), command(`harness pairing-requests deny ${row.id}`));
      }
    }
    if (row.kind === "app" && row.state === "redeemed" && row.key_id) {
      box.append(command(`harness keys revoke ${row.key_id}`));
    }
    return box;
  }
  function decision(row, operation) {
    const draft = drafts.get(row.id) || { match: "", ack: false };
    drafts.set(row.id, draft);
    const elevated = operation === "approve" && row.elevated.length > 0;
    const label = operation === "approve" ? "Approve" : "Confirm";
    // Never pre-filled from the listing: the owner types what the app's own screen shows.
    const input = h("input", { type: "text", inputmode: "numeric", autocomplete: "off", spellcheck: "false",
      maxlength: "32", "data-field": "match", "aria-describedby": `match-help-${row.id}` });
    input.value = draft.match;
    const ack = elevated ? h("input", { type: "checkbox", "data-field": "ack" }) : null;
    if (ack) ack.checked = draft.ack;
    const submit = button(`${label} ${row.name || row.id}`, () => {
      if (submit.disabled) return;
      const body = { match: input.value.trim() };
      if (elevated) body.acknowledge_elevated = true;
      perform(async () => {
        await api(`pairing/${row.id}/${operation}`, { method: "POST", body });
        drafts.delete(row.id);
        message(operation === "approve" ? `Approved ${row.name || row.id}. The app now fetches its own token.` :
          `Confirmed ${row.name || row.id}. The app now fetches its own token.`);
      });
    }, { class: "btn primary", "data-action": operation });
    const update = () => {
      draft.match = input.value;
      if (ack) draft.ack = ack.checked;
      submit.disabled = busy || !input.value.trim() || Boolean(ack && !ack.checked);
    };
    input.addEventListener("input", update);
    ack?.addEventListener("change", update);
    update();
    const cli = `harness pairing-requests ${operation} ${row.id} --match <code>` + (elevated ? " --acknowledge-elevated" : "");
    return h("div", { class: "pairing-decision" },
      h("div", { class: "setting-field" }, h("label", {}, "Match code the app shows", input),
        h("p", { class: "setting-meta", id: `match-help-${row.id}` },
          "Type the code the app is showing right now. If the app shows no code or an error, deny.")),
      ack ? h("label", { class: "pairing-ack" }, ack,
        h("span", {}, `I understand the elevated scopes above (${row.elevated.join(", ")}) and still want to approve.`)) : null,
      h("div", { class: "settings-actions" }, submit), command(cli));
  }
  function confirmDeny(row, verb) {
    if (busy) return;
    const name = row.name || row.id;
    const cancel = button("Cancel", () => clear(confirmation));
    const ok = button(`Confirm ${verb.toLowerCase()}`, () => perform(async () => {
      await api(`pairing/${row.id}/deny`, { method: "POST", body: { confirm: true } });
      message(`${verb === "Deny" ? "Denied" : "Withdrew"} ${name}.`);
    }), { class: "btn primary" });
    clear(confirmation, h("section", { class: "card warn pairing-confirm", role: "alert" },
      h("h2", {}, `${verb} ${name}?`),
      h("p", {}, `${verb === "Deny" ? "Deny" : "Withdraw"} pairing request ${row.id} from ` +
        `${row.origin || "a native app"}. The app cannot use it afterwards; it must ask again.`),
      h("div", { class: "settings-actions" }, ok, cancel), command(`harness pairing-requests deny ${row.id}`)));
    ok.focus();
  }
  async function perform(action) {
    if (busy || stopped) return;
    busy = true;
    clearTimeout(timer);
    for (const control of groups.querySelectorAll("button,input")) control.disabled = true;
    clear(confirmation);
    try {
      await action();
    } catch (error) {
      if (stopped) return;
      if (error.status === 409) message("That request changed state before your decision. The list was refreshed; check it again.", true);
      else if (error.status === 404) message("That request no longer exists. The list was refreshed.", true);
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
