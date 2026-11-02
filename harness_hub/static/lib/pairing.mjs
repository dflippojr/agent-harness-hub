// Shared by Pairing requests and Marketplace: the page controller, the countdown chip on a request or slot, the typed
// match-code field, and the recovery steps for an app that says its slot was already claimed (inspect the current state
// before acting).
import { api } from "./api.mjs";
import { blockedKey, button, command, countdown, h, icon, pill, reveal } from "./dom.mjs";
import { poller } from "./poll.mjs";

export const POLL_MS = 5000;

// A page of requests: its notice and problem lines, the polled read, the redraw when the read changed, focus kept across
// it, and an action with its refusal. The page gives what it reads (path), the node its cards are in (root), the
// attributes that name a card (owners), what an action disables (controls) and hooks: watched(data), the parts of a read
// a redraw shows; onRead(error), after each read; paint(), which redraws inside keepFocus; onAction(), as an action
// starts; refusals, a refused action's message by status; and onRefused(error), whose returned step runs after the
// reload in place of moving focus to the notice.
export function requestPage({ path, root, owners, controls, watched, onRead, paint, onAction, refusals, onRefused }) {
  let stopped = false, offset = 0, signature = "";
  const ownerSelector = owners.map((name) => `[${name}]`).join(", ");
  const poll = poller(() => load());
  const notice = h("p", { class: "notice", "aria-live": "polite", hidden: true, "data-field": "notice" });
  const problem = h("p", { class: "notice bad", role: "alert", hidden: true });
  const page = { notice, problem, data: null, readError: null, busy: false, acted: null,
    message, now, load, draw, keepFocus, focus, perform, stop };
  const ticker = setInterval(() => tickExpiries(root, now()), 1000);

  function message(value, bad = false) {
    notice.textContent = value;
    notice.className = bad ? "notice bad" : "notice";
    notice.hidden = !value;
  }
  function now() { return Date.now() / 1000 + offset; }
  async function load(force = false) {
    if (stopped || page.busy && !force) return;
    poll.cancel();
    try {
      const result = await api(path);
      if (stopped) return;
      page.data = result;
      page.readError = null;
      offset = typeof result.now === "number" ? result.now - Date.now() / 1000 : 0;
    } catch (error) {
      if (stopped) return;
      page.readError = error;
    }
    onRead(page.readError);
    draw(force);
    poll.schedule(POLL_MS);
  }
  function draw(force) {
    const next = JSON.stringify([...watched(page.data), blockedKey(page.readError)]);
    if (!force && next === signature) return;
    signature = next;
    paint();
    tickExpiries(root, now());
  }
  // Keep the owner's typing and focus across a redraw. A control inside `kept`, a node put back as it is, keeps its own
  // focus; any other is found again by its card and its data-field.
  function keepFocus(render, kept) {
    const active = document.activeElement;
    const same = kept?.contains(active) ? active : null;
    const focused = !same && active?.dataset?.field && root.contains(active) ?
      [owner(active), active.dataset.field, active.selectionStart] : null;
    render();
    if (same) same.focus();
    else if (focused) focus(...focused);
  }
  // The card a control belongs to, as [attribute, value].
  function owner(el) {
    const box = el.closest(ownerSelector);
    const attribute = box && owners.find((name) => box.hasAttribute(name));
    return attribute ? [attribute, box.getAttribute(attribute)] : [];
  }
  function focus([attribute, value], field, caret) {
    const target = [...root.querySelectorAll("[data-field]")].find((el) => el.dataset.field === field &&
      el.closest(ownerSelector)?.getAttribute(attribute) === value);
    if (!target) return;
    target.focus();
    if (typeof caret === "number" && target.setSelectionRange) target.setSelectionRange(caret, caret);
  }
  // An action on card `id`: its outcome shows in that card, or under the page head once the card is gone.
  async function perform(id, action) {
    if (page.busy || stopped) return;
    page.busy = true;
    page.acted = id;
    poll.cancel();
    for (const control of root.querySelectorAll(controls)) control.disabled = true;
    onAction();
    let refused = false, afterReload = null;
    try {
      await action();
    } catch (error) {
      if (stopped) return;
      refused = true;
      message(refusals[error.status]?.(error) ?? error.message, true);
      afterReload = onRefused?.(error);
    }
    if (stopped) return;
    page.busy = false;
    await load(true);
    if (afterReload) afterReload();
    else if (refused && !stopped) reveal(notice);
  }
  function stop() {
    stopped = true;
    poll.stop();
    clearInterval(ticker);
  }
  return page;
}

// A button whose visible text is a verb; the rest of its accessible name (a request's or slot's name) sits in a span
// the class may hide.
export function verbButton(verb, rest, action, extra = {}, restClass = "visually-hidden") {
  return button([`${verb} `, h("span", { class: restClass }, rest)], action, extra);
}

// A request's or entry's scope tier as a pill.
export const tier = (item) => item.tier === "elevated" ? pill("Elevated", "bad") : pill("Standard", "muted");

// The chip turns warn in the last two minutes, so a code is not typed into a request about to expire.
const WARN_SECONDS = 120;

// A live countdown chip to `expiresAt` (daemon seconds); tickExpiries redraws every chip under a root.
export function expiry(expiresAt, now) {
  const chip = countdown(expiresAt - now, { warnBelow: WARN_SECONDS });
  chip.dataset.expires = String(expiresAt);
  return chip;
}

export function tickExpiries(root, now) {
  for (const chip of root.querySelectorAll("[data-expires]")) chip.replaceWith(expiry(Number(chip.dataset.expires), now));
}

// The match-code field for request or slot `id`: its label, the input and a hint saying what to do instead (`instead`,
// such as "deny") when the app shows no code. Never pre-filled from a listing: the owner types what the app's own
// screen shows right now, and the Hub never shows the code (owner decision, 2026-10-11).
export function matchField(id, value, instead) {
  const help = `match-help-${id}`;
  const input = h("input", { type: "text", class: "match-input", id: `match-${id}`, inputmode: "numeric",
    autocomplete: "off", spellcheck: "false", maxlength: "32", "data-field": "match", "aria-describedby": help });
  input.value = value;
  return { input, nodes: [h("label", { class: "label", for: input.id }, "Match code the app shows"), input,
    h("p", { class: "hint", id: help }, `Type the code the app is showing right now. If the app shows no code or an ` +
      `error, ${instead}.`)] };
}

// Closed by default: it is for the rare app that reports the slot taken, and must not be the loudest thing on a page.
export function pairingRecovery() {
  return h("details", { class: "card troubleshoot pairing-recovery" },
    h("summary", {}, icon("chevron", "icon chevron"), "Troubleshooting: the app says the slot was already claimed"),
    h("div", { class: "troubleshoot-body" },
      h("p", {}, "List pairing requests; another caller may have claimed the slot first. " +
        "For a redeemed row, revoke its key_id. For an approved or claimed row, deny it."),
      command("harness pairing-requests list"),
      command("harness keys revoke <key_id>"),
      command("harness pairing-requests deny <id>")));
}
