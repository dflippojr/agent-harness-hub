// The shared component builders in lib/dom.mjs (Hub design 1, #68), on a minimal fake document.
import nodeTest from "node:test";
import assert from "node:assert/strict";

import { Element } from "./page-fake.mjs";

import * as dom from "../harness_hub/static/lib/dom.mjs";

// The builders reach for document and Node only when called, so each test installs the fakes and puts the originals
// back; this file also runs inside browser-session.test.mjs beside other suites.
const fakeDocument = { createElement: (tag) => new Element(tag), createElementNS: (namespace, tag) => new Element(tag),
  createTextNode: (text) => Object.assign(new Element("#text"), { text }) };
function test(name, fn) {
  nodeTest(name, () => {
    const saved = ["document", "Node"].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
    Object.assign(globalThis, { document: fakeDocument, Node: Element });
    try {
      fn();
    } finally {
      for (const [key, descriptor] of saved) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
    }
  });
}
const find = (el, tag) => el.tag === tag ? el : el.children.map((child) => find(child, tag)).find(Boolean);

test("pill takes its tone from the Stables label set", () => {
  assert.equal(dom.pill("Needs approval").className, "pill warn");
  assert.equal(dom.pill("Waiting for the app").className, "pill run");
  assert.equal(dom.pill("Paired").className, "pill ok");
  assert.equal(dom.pill("Revoked").className, "pill bad");
  assert.equal(dom.pill("Never used").className, "pill muted");
  assert.equal(dom.pill("Something else").className, "pill");
  assert.equal(dom.pill("Healthy", "ok").className, "pill ok");
  assert.equal(dom.pill("Denied").textContent, "Denied");
});

test("section head carries a count badge", () => {
  const head = dom.sectionHead("Needs you", 2, { tone: "warn" });
  assert.equal(head.tag, "h2");
  assert.equal(head.className, "section-head");
  assert.equal(find(head, "span").className, "count warn");
  assert.equal(head.textContent, "Needs you2");
  assert.equal(dom.sectionHead("History", undefined, { level: 3 }).children.length, 1);
  assert.equal(find(dom.sectionHead("History", 0), "span").textContent, "0");
});

test("banner: bad is an alert, the others are status", () => {
  const bad = dom.banner("bad", "The daemon is not answering.", "Changes are paused.");
  assert.equal(bad.className, "banner bad");
  assert.equal(bad.attrs.get("role"), "alert");
  assert.match(bad.textContent, /not answering\.Changes are paused\./);
  assert.equal(dom.banner("warn", "Restart needed").attrs.get("role"), "status");
  const led = dom.banner("bad", "That sign-in link is not valid.", null, null, { lead: "alert" });
  assert.equal(led.children[0].tag, "svg");
  assert.equal(led.children[0].attrs.get("class"), "icon banner-icon");
  assert.equal(dom.banner("info", "Plain").children[0].className, "banner-text");
});

test("toggle is a checkbox with role=switch and reports changes", () => {
  let seen;
  const row = dom.toggle("Show host commands", { checked: true, onchange: (on) => { seen = on; } });
  const input = find(row, "input");
  assert.equal(row.tag, "label");
  assert.equal(input.attrs.get("role"), "switch");
  assert.equal(input.checked, true);
  input.checked = false;
  input.listeners.get("change")();
  assert.equal(seen, false);
});

test("table builds column heads and rows", () => {
  const wrap = dom.table("Apps", ["Name", "State"], [["Phone", dom.pill("Active")]]);
  assert.equal(wrap.className, "table-wrap");
  const th = find(wrap, "th");
  assert.equal(th.attrs.get("scope"), "col");
  assert.equal(find(wrap, "tbody").textContent, "PhoneActive");
});

test("countdown reads time left, warns in the last minute, then expires", () => {
  assert.equal(dom.clock(272), "4:32");
  assert.equal(dom.clock(3725), "1:02:05");
  assert.equal(dom.countdown(272).className, "countdown");
  assert.equal(dom.countdown(272).textContent, "4:32 left");
  assert.equal(dom.countdown(59.6).className, "countdown warn");
  assert.equal(dom.countdown(0).textContent, "Expired");
  assert.equal(dom.countdown(undefined), null);
  // Pairing warns in the last two minutes.
  assert.equal(dom.countdown(119, { warnBelow: 120 }).className, "countdown warn");
  assert.equal(dom.countdown(120, { warnBelow: 120 }).className, "countdown");
});

test("danger zone holds its actions", () => {
  const zone = dom.dangerZone("Revoke this app", "It stops working at once.", dom.h("button", { class: "btn danger" }, "Revoke"));
  assert.equal(zone.className, "danger-zone");
  assert.equal(zone.textContent, "Revoke this appIt stops working at once.Revoke");
});

// Hub design 3 (#70): a CLI-parity command hides behind its Host command button; a host-only step always shows.
const all = (el, tag) => [...(el.tag === tag ? [el] : []), ...el.children.flatMap((child) => all(child, tag))];
const byClass = (el, name) => el.className?.split(" ").includes(name) ? el
  : el.children.map((child) => byClass(child, name)).find(Boolean);

test("a host-only step is a plain command line with a copy button", () => {
  const line = dom.command("harness hub approve r-1 --match 123456");
  assert.equal(line.className, "command");
  assert.equal(find(line, "code").textContent, "harness hub approve r-1 --match 123456");
  assert.equal(find(line, "button").attrs.get("aria-label"), "Copy: harness hub approve r-1 --match 123456");
});

test("a parity command waits behind a Host command button that controls its lines", () => {
  const box = dom.command("harness keys revoke k-parity", { parity: true });
  assert.equal(box.className, "host-command");
  const [toggle, copy] = all(box, "button");
  assert.equal(toggle.className, "btn host-command-toggle");
  assert.equal(toggle.textContent, "Host command");
  assert.equal(toggle.attrs.get("aria-expanded"), "false");
  const lines = byClass(box, "host-command-lines");
  assert.equal(toggle.attrs.get("aria-controls"), lines.attrs.get("id"));
  assert.equal(find(lines, "code").textContent, "harness keys revoke k-parity");
  assert.equal(copy.attrs.get("aria-label"), "Copy: harness keys revoke k-parity");

  toggle.listeners.get("click")();
  assert.equal(box.className, "host-command open");
  assert.equal(toggle.attrs.get("aria-expanded"), "true");
  // A poll's redraw builds the same command again: it stays open, under a fresh id.
  const redrawn = dom.command("harness keys revoke k-parity", { parity: true });
  assert.equal(redrawn.className, "host-command open");
  assert.equal(find(redrawn, "button").attrs.get("aria-expanded"), "true");
  assert.notEqual(byClass(redrawn, "host-command-lines").attrs.get("id"), lines.attrs.get("id"));

  find(redrawn, "button").listeners.get("click")();
  assert.equal(dom.command("harness keys revoke k-parity", { parity: true }).className, "host-command");
});

test("two controls for the same command each open on their own press", () => {
  // A card's Deny command, and its confirmation's, built while the card's is still closed.
  const card = dom.command("harness pairing-requests deny pr-both", { parity: true });
  const confirmation = dom.command("harness pairing-requests deny pr-both", { parity: true });
  find(card, "button").listeners.get("click")();
  find(confirmation, "button").listeners.get("click")();
  assert.equal(card.className, "host-command open");
  assert.equal(confirmation.className, "host-command open");
  assert.equal(find(confirmation, "button").attrs.get("aria-expanded"), "true");
  find(card, "button").listeners.get("click")();
  find(confirmation, "button").listeners.get("click")();
});

test("one Host commands button reveals an action's several commands; key keeps a changing command open", () => {
  const box = dom.command(["harness config schema", "harness config show"], { parity: true });
  assert.equal(find(box, "button").textContent, "Host commands");
  assert.deepEqual(all(box, "code").map((code) => code.textContent), ["harness config schema", "harness config show"]);

  find(dom.command("harness arm --origin <origin>", { parity: true, key: "arm test.example" }), "button")
    .listeners.get("click")();
  assert.equal(dom.command("harness arm --origin https://a.test", { parity: true, key: "arm test.example" }).className,
    "host-command open");
});
