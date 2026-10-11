// The console's sections. Until a section ships in the Hub, its page lists the CLI commands that do the same thing:
// every Hub action is also on the daemon's CLI (agent-harness docs/management-parity.md).

import { clear, command, h } from "../lib/dom.mjs";

export const SECTIONS = [
  { id: "overview", label: "Overview", ready: true },
  {
    id: "settings", label: "Settings", ready: true,
    about: "The daemon settings registry: view, validate, save, roll back and restart.",
    cli: ["harness config schema", "harness config show", "harness config validate <key>=<value>",
      "harness config set <key>=<value>", "harness config rollback --confirm", "harness config restart --confirm"],
  },
  {
    id: "modules", label: "Modules", ready: true,
    about: "Every daemon module with its presence, switch state and status.",
    cli: ["harness hub status", "harness config set <module>.enabled=<true|false>"],
  },
  {
    id: "apps", label: "Apps", ready: true,
    about: "Paired apps with connection state, last use, token age, sessions, usage and errors; revoke and restore.",
    cli: ["harness hub status", "harness keys list", "harness keys revoke <kid>", "harness apps restore <app_id>"],
  },
  {
    id: "pairing", label: "Pairing requests",
    about: "Apps asking to pair: name, scopes, origin, match code and what each scope allows.",
    cli: ["harness pairing-requests list", "harness pairing-requests approve <rid> --match <code>",
      "harness pairing-requests confirm <rid> --match <code>", "harness pairing-requests deny <rid>"],
  },
  {
    id: "marketplace", label: "Marketplace",
    about: "The local Hub entries and whether each is paired. Being listed here grants nothing.",
    cli: ["harness hub status", "harness hub entries validate",
      "harness pairing-requests arm <catalog_app_id> --scopes <scope>"],
  },
  {
    id: "admin", label: "Accounts and maintenance",
    about: "Household accounts, skills, maintenance and runners.",
    cli: ["harness accounts list", "harness skills list", "harness maintenance status", "harness runner list"],
  },
];

export function renderSection(main, section) {
  clear(main,
    h("header", { class: "page-head" }, h("h1", {}, section.label)),
    h("section", { class: "card", "aria-labelledby": "soon-title" },
      h("h2", { id: "soon-title" }, "Not in this Hub version yet"),
      h("p", {}, section.about),
      h("p", { class: "muted" }, "Until it ships here, the daemon's CLI does the same on the daemon host:"),
      section.cli.map((line) => command(line))));
  return null;
}
