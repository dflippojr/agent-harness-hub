// Shared by Pairing requests and Marketplace: the countdown chip on a request or slot, the typed match-code field, and
// the recovery steps for an app that says its slot was already claimed (inspect the current state before acting).
import { button, command, countdown, h, icon, pill } from "./dom.mjs";

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
