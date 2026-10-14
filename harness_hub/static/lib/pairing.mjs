// Shared by Pairing requests and Marketplace: the countdown chip on a request or slot, and the recovery steps for an
// app that says its slot was already claimed (inspect the current state before acting).
import { clear, clock, command, h, icon } from "./dom.mjs";

// The chip turns warn in the last two minutes, so a code is not typed into a request about to expire.
const WARN_SECONDS = 120;

// A live countdown chip to `expiresAt` (daemon seconds); tickExpiries redraws every chip under a root.
export function expiry(expiresAt, now) {
  const chip = h("span", { class: "countdown", "data-expires": expiresAt });
  paint(chip, now);
  return chip;
}

export function tickExpiries(root, now) {
  for (const chip of root.querySelectorAll("[data-expires]")) paint(chip, now);
}

function paint(chip, now) {
  const left = Number(chip.dataset.expires) - now;
  chip.className = left > 0 && left < WARN_SECONDS ? "countdown warn" : "countdown";
  clear(chip, icon("clock"), left > 0 ? `${clock(left)} left` : "Expired");
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
