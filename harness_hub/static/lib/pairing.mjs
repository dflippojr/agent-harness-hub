// Recovery is shared by Pairing requests and Marketplace; inspect the current state before acting.
import { command, h } from "./dom.mjs";

export function pairingRecovery() {
  return h("section", { class: "notice warn pairing-recovery" },
    h("h2", {}, "If the app says the slot was already claimed"),
    h("p", {}, "List pairing requests; another caller may have claimed the slot first. " +
      "For a redeemed row, revoke its key_id. For an approved or claimed row, deny it."),
    command("harness pairing-requests list"),
    command("harness keys revoke <key_id>"),
    command("harness pairing-requests deny <id>"));
}
