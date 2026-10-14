// The shared component builders in lib/dom.mjs (Hub design 1, #68), on a minimal fake document.
import test from "node:test";
import assert from "node:assert/strict";

import { Element } from "./page-fake.mjs";

globalThis.Node = Element;
globalThis.document = { createElement: (tag) => new Element(tag), createTextNode: (text) => Object.assign(new Element("#text"), { text }) };

const dom = await import("../harness_hub/static/lib/dom.mjs");
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
});

test("danger zone holds its actions", () => {
  const zone = dom.dangerZone("Revoke this app", "It stops working at once.", dom.h("button", { class: "btn danger" }, "Revoke"));
  assert.equal(zone.className, "danger-zone");
  assert.equal(zone.textContent, "Revoke this appIt stops working at once.Revoke");
});
