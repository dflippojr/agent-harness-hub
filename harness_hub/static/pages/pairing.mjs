// Pairing requests (agent-harness #519, Hub design 6): Needs you first, one card per decision in two halves (who and
// what on the left; the typed match code and Deny or Approve on the right), then Waiting for the app, then History as a
// table. The owner types the App code and approves, confirms or denies; the daemon then hands each App its own token,
// and this page never sees one. Every daemon string goes in as a text node.

import { api } from "../lib/api.mjs";
import { blocked, blockedKey, blockedState, clear, command, emphasis, h, icon, pill, placeNotice, reveal, sectionHead, table } from "../lib/dom.mjs";
import { expiry, matchField, pairingRecovery, tickExpiries } from "../lib/pairing.mjs";
import { poller } from "../lib/poll.mjs";

const NEEDS_YOU = ["pending", "claimed"];
const WAITING = ["armed", "approved"];
const ACTIVE = [...NEEDS_YOU, ...WAITING];
const LABELS = { pending: "Needs approval", claimed: "Needs confirmation", armed: "Waiting for the app",
  approved: "Approved", redeemed: "Paired", denied: "Denied", expired: "Expired" };
const POLL_MS = 5000;

const label = (row) => LABELS[row.state] || "Unknown";
const kind = (row) => row.kind === "hub" ? "Hub claim" : row.kind === "app" ? "App" : "Unknown";
const source = (row) => (row.origin || "Native app") + (row.catalog_app_id ? ` · catalog ${row.catalog_app_id}` : "");
const withheld = (row) => [row.name, row.origin, row.catalog_app_id].some((value) => value?.startsWith("[withheld:"));

function stamp(seconds) {
  return typeof seconds === "number" ? new Date(seconds * 1000).toLocaleString(undefined,
    { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
}

// What the app (or Hub) does next, for a request that waits on it rather than on the owner.
function waitingFor(row) {
  if (row.kind === "hub") return "Approved: the Hub fetches its own key";
  return row.state === "armed" ? "Waiting for the app to claim this slot" :
    "Waiting for the app to fetch its own token from the daemon";
}

// Said once per card instead of inside every label: who stated the name, origin and catalog id.
function caveat(row) {
  if (row.kind === "hub") {
    return [icon("hub"), "Hub claim: the name is supplied by the requester. It is decided only on the daemon host, with " +
      "the code from that Hub's own screen."];
  }
  if (row.kind !== "app") return [icon("alert"), "The Hub does not know this kind of request. Review it on the daemon host."];
  if (row.armed) {
    return [icon("check"), "You armed this slot and set its origin and catalog app id; an app has claimed it. " +
      "Redemption is waiting for confirmation: confirm only if the code matches the app on your screen."];
  }
  return [icon("shield"), "Name, origin and catalog id are stated by the app, not verified. Only the match code on " +
    "your app's screen proves this is your app."];
}

export function renderPairing(main) {
  let stopped = false, busy = false, ticker, data = null, offset = 0, signature = "";
  let confirming = null; // the request whose Deny or Withdraw is asking for confirmation, inside its own card
  let acted = null; // the request the last decision acted on: its card holds the notice while it is on the page
  let readError = null; // why the last read failed, or null once one succeeds
  const drafts = new Map();
  const poll = poller(() => load());
  const updated = h("span", { class: "muted refreshed" });
  const notice = h("p", { class: "notice", "aria-live": "polite", hidden: true, "data-field": "notice" });
  const problem = h("p", { class: "notice bad", role: "alert", hidden: true });
  const groups = h("div", { class: "pairing-groups" });
  const refresh = h("button", { type: "button", class: "btn ghost refresh", onclick: () => load(true) },
    icon("refresh"), "Refresh requests");
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Pairing requests"), updated, refresh),
    h("p", { class: "lede" }, "Apps asking to pair in the last day. Approving changes only the request's state: " +
      "the app then fetches its own token from the daemon, and this Hub never sees it."),
    notice, problem, groups, pairingRecovery());

  function button(text, action, extra = {}) {
    return h("button", { type: "button", class: "btn", onclick: action, ...extra }, text);
  }
  // A button whose visible text is a verb; the request's name completes its accessible name (and, where the class
  // allows, shows beside the verb).
  function verbButton(verb, name, action, extra = {}, nameClass = "visually-hidden") {
    return button([`${verb} `, h("span", { class: nameClass }, name)], action, extra);
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
  async function load(force = false) {
    if (stopped || busy && !force) return;
    poll.cancel();
    try {
      const result = await api("pairing");
      if (stopped) return;
      data = result;
      offset = typeof result.now === "number" ? result.now - Date.now() / 1000 : 0;
      readError = null;
      showProblem(result.confirmation_warning || "");
      updated.textContent = `Updated ${new Date().toLocaleTimeString()} · every ${POLL_MS / 1000} s`;
    } catch (error) {
      if (stopped) return;
      readError = error;
      // With no list yet, a read that could not run at all is shown as its state in place of the list.
      showProblem(!data && blocked(error) ? "" : error.message + (data ? " Showing the last list." : ""));
    }
    draw(force);
    poll.schedule(POLL_MS);
  }
  function draw(force) {
    const next = JSON.stringify([data?.requests ?? null, blockedKey(readError)]);
    if (!force && next === signature) return;
    signature = next;
    // Keep the owner's typing and focus across a poll that changed the list.
    const active = document.activeElement;
    const focused = active?.dataset?.field ? [active.closest("[data-request-id]")?.dataset.requestId,
      active.dataset.field, active.selectionStart] : null;
    const rows = data?.requests || [];
    if (confirming && !rows.some((row) => row.id === confirming && ACTIVE.includes(row.state))) confirming = null;
    const needs = rows.filter((row) => NEEDS_YOU.includes(row.state));
    const waiting = rows.filter((row) => WAITING.includes(row.state));
    const done = rows.filter((row) => !ACTIVE.includes(row.state));
    clear(groups,
      needs.length ? group("needs", sectionHead("Needs you", needs.length, { tone: "warn" }), needs.map(decisionCard)) : null,
      waiting.length ? group("waiting", sectionHead("Waiting for the app", waiting.length), waiting.map(waitingRow)) : null,
      done.length ? group("history", sectionHead("History · last day", done.length), history(done)) : null,
      rows.length ? null : empty());
    // In a decision card the notice joins the decision half, by the buttons it answers.
    const card = acted && [...groups.querySelectorAll("section[data-request-id]")]
      .find((el) => el.dataset.requestId === acted);
    placeNotice(notice, card ? card.querySelector(".decision-act") || card : null, problem);
    if (focused) focus(focused[0], focused[1], focused[2]);
    tickExpiries(groups, now());
  }
  function focus(id, field, caret) {
    const target = [...groups.querySelectorAll("[data-field]")].find((el) =>
      el.dataset.field === field && el.closest("[data-request-id]")?.dataset.requestId === id);
    if (!target) return;
    target.focus();
    if (typeof caret === "number" && target.setSelectionRange) target.setSelectionRange(caret, caret);
  }
  function group(name, head, content) {
    return h("section", { class: "pairing-group", "data-group": name }, head, content);
  }
  function empty() {
    if (!data) {
      return blockedState(readError, "Pairing requests", () => load(true)) ??
        h("p", { class: "muted" }, "The pairing list is not available.");
    }
    return h("section", { class: "card empty" }, icon("pairing"), h("h2", {}, "No pairing requests in the last day"),
      h("p", {}, "Apps start pairing from their own setup screen, or you arm a slot from Marketplace. " +
        "New requests appear here within 5 seconds."),
      h("a", { class: "btn", href: "#/marketplace" }, "Open Marketplace"));
  }
  function countdown(row) {
    return typeof row.expires_at === "number" ? expiry(row.expires_at, now()) : null;
  }
  function facts(rows) {
    return h("dl", { class: "facts" }, rows.map(([key, value]) => [h("dt", {}, key), h("dd", {}, value)]));
  }
  function tier(item) {
    return item.tier === "elevated" ? pill("Elevated", "bad") : pill("Standard", "muted");
  }

  // Needs you: who and what, then the decision. On a phone the decision comes right after the head and its scope chips.
  function decisionCard(row) {
    const name = row.name || row.id;
    const heading = `request-${row.id}`;
    return h("section", { class: "card decision", "data-request-id": row.id, "data-state": row.state,
      "aria-labelledby": heading },
    h("div", { class: "decision-head" }, h("h3", { id: heading }, name), pill(label(row)),
      row.elevated.length ? pill("Elevated scope", "bad") : null, countdown(row)),
    h("div", { class: "chips decision-scopes", "aria-hidden": "true" }, row.scopes.map((scope) =>
      row.elevated.includes(scope) ? pill(`${scope} · elevated`, "bad") : pill(scope, "muted"))),
    about(row), h("div", { class: "decision-act" }, confirming === row.id ? confirmation(row) : act(row, name)));
  }
  function about(row) {
    const owned = row.kind === "app" && row.armed;
    // Every requested scope, whether or not the daemon described it: the owner approves the whole list.
    const scopes = ["Scopes", row.scopes.length ? h("span", { class: "chips" }, row.scopes.map((scope) =>
      h("span", { class: "scope-chip" },
        tier({ tier: row.elevated.includes(scope) ? "elevated" : "standard" }), " ", scope))) : "None"];
    const rows = owned ? [["Slot", h("span", { class: "mono" }, row.id)], ["Bound to", `${source(row)} (set by you)`], scopes] :
      [["Request", h("span", { class: "mono" }, row.id)],
        ["From", row.kind === "hub" ? "Hub claim" : source(row)],
        row.kind === "app" ? ["Started from", `The app · ${stamp(row.created_at)}`] : ["Created", stamp(row.created_at)],
        scopes];
    if (row.key_id) rows.push(["Key id", h("span", { class: "mono" }, row.key_id)]);
    if (!row.disclosures.length) return h("div", { class: "decision-about" }, h("p", { class: "caveat" }, caveat(row)), facts(rows));
    const list = h("ul", { class: "disclosures" }, row.disclosures.map((item) => h("li", { "data-scope": item.scope },
      h("p", { class: "disclosure-head" }, tier(item), " ", h("code", {}, item.scope)),
      h("p", { class: "disclosure-text" }, emphasis(item.text || "The daemon supplied no description for this scope.")))));
    // You consented to an armed slot's scopes when you armed it, so their text starts closed; it is still one tap away.
    return h("div", { class: "decision-about" }, h("p", { class: "caveat" }, caveat(row)), facts(rows),
      owned ? h("details", { class: "scope-details" },
        h("summary", { class: "card-sub" }, icon("chevron", "icon chevron"), "What each scope allows"), list) :
        [h("h4", { class: "card-sub" }, "What each scope allows"), list]);
  }
  function act(row, name) {
    if (row.kind === "hub") {
      return [h("p", {}, "Hub claims are decided only on the daemon host. Read the code off that Hub's own screen."),
        command(`harness hub approve ${row.id} --match <code>`), command(`harness hub deny ${row.id}`)];
    }
    if (row.kind !== "app") return [command("harness pairing-requests list")];
    const deny = verbButton("Deny", name, () => askDeny(row), { class: "btn danger", "data-action": "deny", "data-field": "deny" });
    if (withheld(row)) {
      return [h("p", { class: "notice bad" }, "Review this request with harness pairing-requests list on the host."),
        command("harness pairing-requests list"), h("div", { class: "decision-buttons" }, deny),
        command(`harness pairing-requests deny ${row.id}`, { parity: true })];
    }
    return decision(row, name, row.state === "pending" ? "approve" : "confirm", deny);
  }
  function decision(row, name, operation, deny) {
    const draft = drafts.get(row.id) || { match: "", ack: false };
    drafts.set(row.id, draft);
    const elevated = operation === "approve" && row.elevated.length > 0;
    const field = matchField(row.id, draft.match, "deny");
    const input = field.input;
    const ack = elevated ? h("input", { type: "checkbox", "data-field": "ack" }) : null;
    if (ack) ack.checked = draft.ack;
    const submit = verbButton(operation === "approve" ? "Approve" : "Confirm", name, () => {
      if (submit.disabled) return;
      const body = { match: input.value.trim() };
      if (elevated) body.acknowledge_elevated = true;
      void perform(row.id, async () => {
        await api(`pairing/${row.id}/${operation}`, { method: "POST", body });
        drafts.delete(row.id);
        message(`${operation === "approve" ? "Approved" : "Confirmed"} ${name}. The app now fetches its own token.`);
      });
    }, { class: "btn primary", "data-action": operation }, "button-name");
    const update = () => {
      draft.match = input.value;
      if (ack) draft.ack = ack.checked;
      submit.disabled = busy || !input.value.trim() || Boolean(ack && !ack.checked);
    };
    input.addEventListener("input", update);
    ack?.addEventListener("change", update);
    update();
    const scopes = row.elevated.length === 1 ? "scope" : "scopes";
    const cli = `harness pairing-requests ${operation} ${row.id} --match <code>` + (elevated ? " --acknowledge-elevated" : "");
    return [field.nodes,
      ack ? h("label", { class: "decision-ack" }, ack,
        h("span", {}, `I understand the elevated ${scopes} (${row.elevated.join(", ")}) and still want to approve.`)) : null,
      h("div", { class: "decision-buttons" }, deny, submit),
      command([cli, `harness pairing-requests deny ${row.id}`], { parity: true })];
  }

  // Waiting for the app: one row each, with the time left and, for an App, Withdraw.
  function waitingRow(row) {
    const name = row.name || row.id;
    const heading = `request-${row.id}`;
    const app = row.kind === "app";
    const withdraw = app && confirming !== row.id ? verbButton("Withdraw", name, () => askDeny(row),
      { class: "btn withdraw", "data-action": "deny", "data-field": "deny" }) : null;
    return h("section", { class: "card waiting", "data-request-id": row.id, "data-state": row.state,
      "aria-labelledby": heading },
    h("div", { class: "waiting-main" },
      h("div", { class: "waiting-text" },
        h("div", { class: "waiting-title" }, h("h3", { id: heading }, name), pill(label(row))),
        h("p", { class: "muted" }, `${waitingFor(row)} · ${row.scopes.join(", ") || "no scopes"}`)),
      countdown(row), withdraw),
    confirming === row.id ? confirmation(row) :
      app ? command(`harness pairing-requests deny ${row.id}`, { parity: true }) : null);
  }

  function history(rows) {
    return h("div", { class: "card pairing-history" }, table("Finished pairing requests",
      ["Name", "Kind", "State", "Scopes", "Finished", "Key"], rows.map((row) =>
        h("tr", { "data-request-id": row.id, "data-state": row.state },
          h("td", { class: "history-name" }, row.name || row.id), h("td", {}, kind(row)), h("td", {}, pill(label(row))),
          h("td", {}, row.scopes.join(", ") || "None"), h("td", { class: "nowrap" }, stamp(row.finished_at)),
          h("td", { class: row.key_id ? "mono nowrap" : "muted" }, row.key_id || "—")))));
  }

  // Deny and Withdraw ask first, inside the card that was acted on (decision 5 of #65).
  function askDeny(row) {
    if (busy) return;
    confirming = row.id;
    draw(true);
    focus(row.id, "confirm-deny");
  }
  function confirmation(row) {
    const name = row.name || row.id;
    const verb = NEEDS_YOU.includes(row.state) ? "Deny" : "Withdraw";
    const cancel = button("Cancel", () => {
      confirming = null;
      draw(true);
      focus(row.id, "deny");
    }, { "data-field": "cancel" });
    const ok = button(`Confirm ${verb.toLowerCase()}`, () => perform(row.id, async () => {
      await api(`pairing/${row.id}/deny`, { method: "POST", body: { confirm: true } });
      message(`${verb === "Deny" ? "Denied" : "Withdrew"} ${name}.`);
    }), { class: "btn destructive", "data-field": "confirm-deny" });
    return h("div", { class: "decision-confirm", role: "alert" },
      h("h4", {}, `${verb} ${name}?`),
      h("p", {}, `${verb} ${row.kind === "app" && row.armed ? "slot" : "pairing request"} ${row.id} from ` +
        `${row.origin || "a native app"}. The app cannot use it afterwards; it must ask again.`),
      h("div", { class: "decision-buttons" }, cancel, ok),
      command(`harness pairing-requests deny ${row.id}`, { parity: true }));
  }
  // A decision on request `id`: its outcome shows in that request's card, or under the page head once the card is gone.
  async function perform(id, action) {
    if (busy || stopped) return;
    busy = true;
    acted = id;
    poll.cancel();
    for (const control of groups.querySelectorAll("button,input")) control.disabled = true;
    confirming = null;
    let refused = false;
    try {
      await action();
    } catch (error) {
      if (stopped) return;
      refused = true;
      if (error.status === 409) message("That request changed state before your decision. The list was refreshed; check it again.", true);
      else if (error.status === 404) message("That request no longer exists. The list was refreshed.", true);
      else message(error.message, true);
    }
    if (stopped) return;
    busy = false;
    await load(true);
    if (refused && !stopped) reveal(notice);
  }
  ticker = setInterval(() => tickExpiries(groups, now()), 1000);
  load(true);
  return () => { stopped = true; poll.stop(); clearInterval(ticker); };
}
