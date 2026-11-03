// Marketplace (agent-harness #520, #519 flow 4; Hub design 10): the owner's local Hub entries, and setting an app up
// from one by arming a slot with scope consent after a consent screen. Listing grants nothing. Each entry is one card:
// Set up opens the consent inside that card, the hand-off replaces it there, and the entry's slots show in the card
// with the same countdown chip and match-code field as Pairing requests. Nothing named by an entry is fetched: the only
// image shown is an inline one the server already checked. Every daemon string goes in as a text node.

import { api } from "../lib/api.mjs";
import { banner, blocked, blockedState, button, clear, command, emphasis, facts, h, icon, pill, placeNotice,
  sectionHead, sentence, when } from "../lib/dom.mjs";
import { expiry, matchField, pairingRecovery, requestPage, tier, verbButton } from "../lib/pairing.mjs";

const STATES = { paired: ["Paired", "ok"], not_paired: ["Not paired", "muted"], pending: ["Pending", "warn"] };
const SLOT_LABELS = { armed: "Waiting for the app", claimed: "Needs confirmation", approved: "Approved" };
const SLOT_TEXT = { armed: "Armed: waiting for the app to claim it.",
  claimed: "An app claimed this slot. Redemption is waiting for confirmation.",
  approved: "Approved: waiting for the app to fetch its own token from the daemon." };

// The scopes whose acknowledgement the consent screen asks for: the daemon may require it for a scope the Hub rates standard.
export function acknowledgedScopes(entry) {
  return entry.scopes.filter((s) => entry.acknowledgement_required || s.tier === "elevated" || !s.text).map((s) => s.scope);
}

export function armCommand(entry, origin) {
  return `harness pairing-requests arm ${entry.catalog_app_id} --scopes ${entry.scopes.map((s) => s.scope).join(" ")}` +
    (origin ? ` --origin ${origin}` : "") + (acknowledgedScopes(entry).length ? " --acknowledge-elevated" : "");
}

export function renderMarketplace(main) {
  // { id, node, handoff }: the consent or hand-off open inside the card of entry `id`. A hand-off stays until Done,
  // even if its entry leaves the list: the slot is armed, and the owner still needs its request id.
  let open = null;
  let confirming = null; // the slot whose Withdraw is asking for confirmation, inside its own card (decision 5 of #65)
  const drafts = new Map();
  const count = h("div");
  const list = h("div", { class: "market-list" });
  const page = requestPage({ path: "marketplace", root: list, owners: ["data-slot", "data-entry"], controls: "button:not(.copy), input",
    watched: (data) => [data?.entries ?? null, data?.withheld ?? 0], onRead, paint, onRefused,
    onAction: () => { confirming = null; },
    refusals: { 409: (error) => error.message + " The entries were refreshed; check them again.",
      404: (error) => error.message + " The entries were refreshed." } });
  const { notice, problem, message, now, load, perform, draw, keepFocus, focus } = page;
  const refresh = h("button", { type: "button", class: "btn ghost refresh", onclick: () => load(true) },
    icon("refresh"), "Refresh entries");
  clear(main, h("header", { class: "page-head" }, h("h1", {}, "Marketplace"), refresh),
    banner("info", h("strong", {}, "Being listed here grants nothing."), "Entries come from the daemon host's entries " +
      "file; none is verified. Setting one up arms a slot with your scope consent. Browser and native apps claim once, " +
      "then wait for your typed-code confirmation before fetching their own token from the daemon, and this Hub never " +
      "sees it.", null, { lead: "info" }),
    command(["harness hub status", "harness hub entries validate"], { parity: true }), notice, problem, count, list,
    pairingRecovery());

  function onRead(error) {
    const entries = page.data?.entries;
    // With no entries yet, a read that could not run at all is shown as its state in place of the list.
    showProblems(error && (entries || !blocked(error)) ?
      error.message + (entries ? " Showing the last entries this page read." : "") : "");
  }
  function showProblems(failure) {
    const { data } = page;
    const lines = [];
    if (failure) lines.push(failure);
    else if (data && data.state !== "ready") {
      lines.push(sentence(data.error) || "The entries could not be read.");
      lines.push(data.entries ? `Showing the last good entries, read ${when(data.fetched_at)}.` :
        "There are no last good entries to show.");
    }
    if (!failure && data?.slots_error) lines.push(sentence(data.slots_error));
    if (data?.confirmation_warning) lines.push(data.confirmation_warning);
    problem.textContent = lines.join(" ");
    problem.hidden = !lines.length;
  }
  function paint() {
    const { data } = page;
    const entries = data?.entries || [];
    const listed = open && entries.some((entry) => entry.catalog_app_id === open.id);
    if (open && !listed && !open.handoff) open = null;
    if (confirming && !entries.some((entry) => entry.slots?.some((slot) => slot.id === confirming))) confirming = null;
    // The open consent is the same node, put back in its new card.
    keepFocus(() => drawEntries(data, entries, listed), open?.node);
  }
  function drawEntries(data, entries, listed) {
    clear(count, data?.entries ? sectionHead("Entries", entries.length) : null);
    clear(list, open && !listed ? h("section", { class: "card market-entry open", "data-entry": open.id }, open.node) : null,
      entries.map(card));
    if (data?.withheld) {
      list.append(h("p", { class: "notice warn" }, `${data.withheld} ${data.withheld === 1 ? "entry was" :
        "entries were"} withheld: the daemon's answer did not match the entries contract.`));
    }
    if (!entries.length) list.append(empty());
    placeNotice(notice, page.acted && [...list.querySelectorAll("[data-entry]")].find((el) => el.dataset.entry === page.acted),
      problem);
  }
  function empty() {
    if (!page.data?.entries) {
      return blockedState(page.readError, "Marketplace entries", () => load(true)) ??
        h("p", { class: "muted" }, "No entries to show.");
    }
    return h("section", { class: "card empty" }, icon("marketplace"), h("h2", {}, "No entries yet"),
      h("p", {}, "The entries file lists no apps. Create it on the daemon host with harness hub entries init."),
      command("harness hub entries init"));
  }
  function entryIcon(entry) {
    // Only a data: raster image survives the server's allowlist, so this never loads anything from the network.
    if (entry.icon.startsWith("data:image/")) {
      return h("img", { class: "entry-icon", src: entry.icon, alt: "", width: 48, height: 48 });
    }
    return h("span", { class: "entry-icon", "aria-hidden": "true" }, (entry.name || entry.catalog_app_id).slice(0, 1));
  }
  function card(entry) {
    const id = entry.catalog_app_id, name = entry.name || id;
    const [label, tone] = STATES[entry.state] || ["Unknown", "muted"];
    const heading = `entry-${id}`;
    const opened = open?.id === id;
    const elevated = (scope) => scope.tier === "elevated" || entry.elevated.includes(scope.scope);
    const origins = entry.browser_origins;
    const native = entry.unverified_origin;
    return h("section", { class: opened ? "card market-entry open" : "card market-entry", "data-entry": id,
      "aria-labelledby": heading },
    h("div", { class: "entry-main" },
      h("div", { class: "entry-head" }, entryIcon(entry),
        h("div", { class: "entry-title" }, h("h2", { id: heading }, name),
          h("p", { class: "muted entry-by" }, [entry.publisher.name || "Unknown publisher",
            entry.version ? `version ${entry.version}` : null, id].filter(Boolean).join(" · "))),
        h("div", { class: "entry-pills" }, pill("Local · not verified", "muted"),
          pill(label, tone, { "data-field": "state" }),
          entry.pending_pairing ? pill("Pending", "warn", { "data-field": "pending" }) : null),
        opened ? null : button(`Set up ${name}`, () => consent(entry),
          { class: "btn primary entry-setup", "data-action": "setup", "data-field": "setup" })),
      entry.summary ? h("p", { class: "entry-summary" }, entry.summary) : null,
      h("dl", { class: "entry-meta" },
        h("div", {}, h("dt", {}, "Scopes"), h("dd", { class: "chips" }, entry.scopes.map((scope) =>
          elevated(scope) ? pill(`${scope.scope} · elevated`, "bad") : pill(scope.scope, "muted")))),
        h("div", {}, h("dt", {}, "Runs as"), h("dd", {}, origins.length ?
          ["Browser app at ", h("span", { class: "mono" }, origins.join(", "))] : "Native app")),
        entry.paired.length ? h("div", {}, h("dt", {}, "Paired keys"),
          h("dd", { class: "mono" }, entry.paired.join(", "))) : null),
      entry.pending_pairing && !entry.slots?.length ? h("p", { class: "hint" },
        "A request naming this app's catalog id is pending on Pairing requests. " +
        "The requester chose that id; it is not verified.") : null,
      native.paired.length || native.pending_pairing ? h("div", { class: "entry-native", "data-field": "unverified" },
        h("h3", {}, "Native keys and requests (origin not verified)"),
        h("p", { class: "hint" }, "These match the catalog id only, so they do not change the entry's state."),
        facts([["Key ids", native.paired.join(", ") || "None"],
          ["Request pending", native.pending_pairing ? "Yes" : "No"]])) : null),
    (entry.slots || []).map((slot) => slotView(entry, slot)),
    opened ? open.node : null);
  }
  function disclosureList(items) {
    return h("ul", { class: "disclosures" }, items.map((item) => h("li", { "data-scope": item.scope },
      h("p", { class: "disclosure-head" }, tier(item), " ", h("code", {}, item.scope)),
      h("p", { class: "disclosure-text" }, emphasis(item.text || "The Hub has no description for this scope. " +
        "The daemon's description appears once the slot is armed.")),
      item.justification ? h("p", { class: "hint" }, "Publisher's reason (not verified): " +
        item.justification) : null)));
  }
  function scopeDetails(items) {
    return h("details", { class: "scope-details" },
      h("summary", {}, icon("chevron", "icon chevron"), "What the daemon says each scope allows"), disclosureList(items));
  }

  // A slot in its entry's card: the same countdown chip as Pairing requests, Withdraw, and for a claimed slot the
  // match-code field with Withdraw and Confirm.
  function slotView(entry, slot) {
    const name = entry.name || entry.catalog_app_id;
    const claimed = slot.state === "claimed", asking = confirming === slot.id;
    const label = SLOT_LABELS[slot.state];
    const withdraw = verbButton("Withdraw", `slot for ${name}`, () => askWithdraw(slot),
      { class: claimed ? "btn danger" : "btn withdraw", "data-action": "withdraw", "data-field": "withdraw" });
    return h("div", { class: "market-slot", "data-slot": slot.id, "data-state": slot.state },
      h("div", { class: "slot-head" }, h("h3", {}, `Slot ${slot.id}`), label ? pill(label) : null,
        typeof slot.expires_at === "number" ? expiry(slot.expires_at, now()) : null, claimed || asking ? null : withdraw),
      h("p", { class: "muted slot-text" }, `Bound to ${slot.origin || "a native app"}. ${SLOT_TEXT[slot.state] || ""}`),
      asking ? withdrawal(entry, slot) : claimed ? confirmation(entry, slot, withdraw) :
        command(`harness pairing-requests deny ${slot.id}`, { parity: true }),
      slot.disclosures.length ? scopeDetails(slot.disclosures) : null);
  }
  function confirmation(entry, slot, withdraw) {
    const name = entry.name || entry.catalog_app_id;
    const field = matchField(slot.id, drafts.get(slot.id) || "", "withdraw this slot");
    const input = field.input;
    const submit = verbButton("Confirm", name, () => {
      if (submit.disabled) return;
      const match = input.value.trim();
      void perform(entry.catalog_app_id, async () => {
        open = null;
        await api(`pairing/${slot.id}/confirm`, { method: "POST", body: { match } });
        drafts.delete(slot.id);
        message(`Confirmed ${name}. The app now fetches its own token from the daemon.`);
      });
    }, { class: "btn primary", "data-action": "confirm", "data-field": "confirm" }, "button-name");
    const update = () => {
      drafts.set(slot.id, input.value);
      submit.disabled = page.busy || !input.value.trim();
    };
    input.addEventListener("input", update);
    submit.disabled = page.busy || !input.value.trim();
    return h("div", { class: "slot-decision" }, field.nodes, h("div", { class: "decision-buttons" }, withdraw, submit),
      command([`harness pairing-requests confirm ${slot.id} --match <code>`, `harness pairing-requests deny ${slot.id}`],
        { parity: true }));
  }
  function askWithdraw(slot) {
    if (page.busy) return;
    confirming = slot.id;
    draw(true);
    focus(["data-slot", slot.id], "confirm-withdraw");
  }
  function withdrawal(entry, slot) {
    const name = entry.name || entry.catalog_app_id;
    const cancel = button("Cancel", () => {
      confirming = null;
      draw(true);
      focus(["data-slot", slot.id], "withdraw");
    }, { "data-field": "cancel" });
    const ok = button("Confirm withdraw", () => perform(entry.catalog_app_id, async () => {
      open = null;
      await api(`pairing/${slot.id}/deny`, { method: "POST", body: { confirm: true } });
      message(`Withdrew the slot for ${name}.`);
    }), { class: "btn destructive", "data-field": "confirm-withdraw" });
    return h("div", { class: "decision-confirm", role: "alert" },
      h("h4", {}, `Withdraw the slot for ${name}?`),
      h("p", {}, `Slot ${slot.id} can no longer be claimed or redeemed. Set the app up again to pair it.`),
      h("div", { class: "decision-buttons" }, cancel, ok),
      command(`harness pairing-requests deny ${slot.id}`, { parity: true }));
  }

  // The consent screen, inside the card of the entry it sets up. It shows the copy the owner reviews (`entry`), which a
  // poll does not change: Approve sends that copy's digest, and the server refuses it if the entry has moved on.
  function consent(entry) {
    if (page.busy) return;
    message("");
    const id = entry.catalog_app_id, name = entry.name || id;
    const origins = entry.browser_origins;
    let origin = origins.length === 1 ? origins[0] : "";
    const cli = h("div");
    const needsAck = acknowledgedScopes(entry);
    const ack = needsAck.length ? h("input", { type: "checkbox", "data-field": "ack" }) : null;
    const approve = button("Approve", () => {
      if (approve.disabled) return;
      const body = { catalog_app_id: id, scopes: entry.scopes.map((s) => s.scope),
        consent_digest: entry.consent_digest };
      if (origin) body.origin = origin;
      if (ack) body.acknowledge_elevated = true;
      void perform(id, async () => {
        const armed = await api("marketplace/arm", { method: "POST", body });
        handoff(id, armed, origin);
      });
    }, { class: "btn primary", "data-action": "approve" });
    const update = () => {
      approve.disabled = page.busy || Boolean(origins.length && !origin) || Boolean(ack && !ack.checked);
      clear(cli, command(armCommand(entry, origin || (origins.length ? "<origin>" : "")),
        { parity: true, key: `arm ${id}` }));
    };
    let where;
    if (origins.length === 1) {
      where = h("p", {}, h("strong", {}, "Browser app."), " The slot is bound to exactly ",
        h("code", { "data-field": "origin" }, origin),
        ". Claims and redemption must use that exact origin; typed-code confirmation is also required.");
    } else if (origins.length) {
      where = h("fieldset", { class: "origin-choice" }, h("legend", {}, "Browser origin to bind the slot to"),
        origins.map((value) => {
          const radio = h("input", { type: "radio", name: "origin", value });
          radio.addEventListener("change", () => { origin = value; update(); });
          return h("label", { class: "pairing-ack" }, radio, h("code", {}, value));
        }),
        h("p", { class: "hint" }, "Claims and redemption must use the chosen exact origin; " +
          "typed-code confirmation is also required."));
    } else {
      where = h("p", {}, h("strong", {}, "Native app."), " When it claims the slot, you confirm it here or on Pairing " +
        "requests with the code the app shows.");
    }
    ack?.addEventListener("change", update);
    const cancel = button("Cancel", () => close(id));
    const heading = `consent-${id}`;
    const node = h("div", { class: "market-consent", role: "region", "aria-labelledby": heading },
      h("h3", { id: heading, tabindex: "-1" }, `Set up ${name} (${id})?`),
      h("p", {}, "Arming grants scope consent. Nothing is armed until you approve. Both browser and native claims " +
        "wait for you to type the code the app shows before it can redeem its token."),
      h("div", { class: "consent-grid" },
        h("div", {}, h("p", { class: "label" }, "Scopes it will get"),
          h("div", { class: "consent-box" }, disclosureList(entry.scopes))),
        h("div", {}, h("p", { class: "label" }, "Where the app runs"), h("div", { class: "consent-box" }, where))),
      ack ? h("label", { class: "decision-ack" }, ack, h("span", {},
        `I understand the scopes above requiring a separate acknowledgement (${needsAck.join(", ")}) ` +
        `and still want to set up ${name}.`)) : null,
      h("div", { class: "consent-actions" }, approve, cancel), cli);
    update();
    show(id, node);
  }
  function handoff(id, armed, origin) {
    const slot = armed.request, name = armed.name || slot.name;
    const heading = `handoff-${id}`;
    const node = h("div", { class: "market-handoff", role: "region", "aria-labelledby": heading },
      h("div", { class: "slot-head" }, icon("check", "icon handoff-check"),
        h("h3", { id: heading, tabindex: "-1" }, `Slot armed for ${name}`)),
      h("p", { class: "lead" }, `Give this only to ${name}; it lets that app claim the slot once.`),
      h("p", { class: "label" }, "Request id"), command(slot.id),
      h("p", { class: "label" }, "Daemon URL"), command(armed.daemon_url),
      h("p", { class: "hint" }, "No secret exists yet. " + (origin ? `The app must claim it from ${origin}; ` +
        "redemption must use that exact origin too. " : "") +
        "When the app claims it, type the code it shows on this entry or Pairing requests to confirm before redemption. " +
        "The slot's countdown shows how long the app has to claim it."),
      scopeDetails(slot.disclosures),
      h("div", { class: "consent-actions" }, button("Done", () => close(id))));
    message(`Armed a slot for ${name}.`);
    show(id, node, true);
  }
  function show(id, node, handoff = false) {
    open = { id, node, handoff };
    draw(true);
    node.querySelector("h3").focus();
  }
  function close(id) {
    open = null;
    draw(true);
    focus(["data-entry", id], "setup");
  }
  // A refused action closes the consent. A 409 that carries the entry as the daemon has it now reopens it after the
  // reload: on the listed copy if it says the same, else on the daemon's, for the owner to review what changed.
  function onRefused(error) {
    open = null;
    if (error.status !== 409 || !error.body.consent_entry) return null;
    const reviewed = error.body.consent_entry;
    return () => {
      const current = page.data?.entries?.find((entry) => entry.catalog_app_id === reviewed.catalog_app_id);
      const sameCopy = current && JSON.stringify(current.scopes) === JSON.stringify(reviewed.scopes) &&
        Boolean(current.acknowledgement_required) === Boolean(reviewed.acknowledgement_required);
      consent(sameCopy ? current : reviewed);
      message(error.message, true);
    };
  }
  void load(true);
  return page.stop;
}
