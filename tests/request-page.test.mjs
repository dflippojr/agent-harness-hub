// What Pairing requests and Marketplace promise alike: a refused action's message, controls back on after the reload,
// focus and caret kept across a poll's redraw, and nothing left running after teardown.
import assert from "node:assert/strict";
import test from "node:test";
import { renderMarketplace } from "../harness_hub/static/pages/marketplace.mjs";
import { renderPairing } from "../harness_hub/static/pages/pairing.mjs";
import { Element, mount, response } from "./page-fake.mjs";

const camel = (name) => name.slice(5).replace(/-(.)/g, (_, letter) => letter.toUpperCase());

// Enough of the DOM for these pages: parents, attribute selectors, and focus that a removed node loses.
class Dom extends Element {
  parentNode = null;
  append(...children) {
    for (const child of children) {
      child.remove();
      child.parentNode = this;
      this.children.push(child);
    }
  }
  remove() {
    const parent = this.parentNode;
    if (!parent) return;
    if (this.contains(document.activeElement)) document.activeElement = null;
    parent.children.splice(parent.children.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceChildren() {
    for (const child of [...this.children]) child.remove();
    this.text = "";
  }
  replaceWith(other) {
    const { parentNode: parent } = this;
    other.remove();
    parent.children[parent.children.indexOf(this)] = other;
    other.parentNode = parent;
    this.parentNode = null;
  }
  get nextSibling() {
    const siblings = this.parentNode?.children ?? [];
    return siblings[siblings.indexOf(this) + 1] ?? null;
  }
  setAttribute(name, value) {
    if (name.startsWith("data-")) this.dataset[camel(name)] = value;
    else super.setAttribute(name, value);
  }
  getAttribute(name) {
    return (name.startsWith("data-") ? this.dataset[camel(name)] : this.attrs.get(name)) ?? null;
  }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  contains(other) {
    for (let node = other; node; node = node.parentNode) if (node === this) return true;
    return false;
  }
  // A comma list of tag, .class, [attribute] and :not(.class) parts: all these pages ask for.
  matches(selector) {
    const classes = String(this.className).split(" ");
    return selector.split(",").some((one) => {
      const [, tag, name, attribute, without] = one.trim().match(/^(\w*)(?:\.([\w-]+))?(?:\[([\w-]+)\])?(?::not\(\.([\w-]+)\))?$/);
      return (!tag || this.tag === tag) && (!name || classes.includes(name)) && (!attribute || this.hasAttribute(attribute)) &&
        !(without && classes.includes(without));
    });
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  setSelectionRange(start) { this.selectionStart = start; }
}

const NOW = 5000;
const row = (id, name) => ({ id, name, kind: "app", state: "pending", scopes: ["sessions"], elevated: [], disclosures: [],
  armed: false, created_at: NOW - 60, expires_at: NOW + 600 });
const slot = (id) => ({ id, state: "claimed", origin: null, expires_at: NOW + 600, disclosures: [] });
const entry = (id, slots = []) => ({ catalog_app_id: id, name: id, publisher: { name: "Someone" }, version: "1", summary: "",
  icon: "", state: "not_paired", elevated: [], browser_origins: [], paired: [], consent_digest: "d", slots,
  scopes: [{ scope: "sessions", tier: "standard", text: "Start tasks." }], unverified_origin: { paired: [], pending_pairing: false } });

const PAGES = [
  { name: "Pairing requests", render: renderPairing, read: "/api/pairing", write: "/api/pairing/r1/approve",
    owner: "data-request-id", card: "[data-request-id]", id: "r1", action: "approve",
    body: (extra) => ({ now: NOW, requests: [row("r1", "Notes"), ...extra.map((id) => row(id, id))] }),
    refused: [[409, "That request changed state before your decision. The list was refreshed; check it again."],
      [404, "That request no longer exists. The list was refreshed."], [500, "The daemon broke."]] },
  { name: "Marketplace", render: renderMarketplace, read: "/api/marketplace", write: "/api/pairing/s1/confirm",
    owner: "data-slot", card: "[data-entry]", id: "s1", action: "confirm",
    body: (extra) => ({ now: NOW, state: "ready", entries: [entry("app", [slot("s1")]), ...extra.map((id) => entry(id))] }),
    refused: [[409, "The daemon broke. The entries were refreshed; check them again."],
      [404, "The daemon broke. The entries were refreshed."], [500, "The daemon broke."]] },
];

function open(t, spec, write = () => response({})) {
  const state = { extra: [] };
  const page = mount(t, spec.render, (url, options) => Promise.resolve(options.method === "POST" ? write(url, options) :
    url === spec.read ? response(spec.body(state.extra)) : response({ detail: "nope" }, 404)), Dom);
  const match = () => page.main.querySelectorAll("input").find((el) => el.dataset.field === "match" &&
    el.closest(`[${spec.owner}]`)?.getAttribute(spec.owner) === spec.id);
  return { ...page, state, match };
}
const type = (input, text) => { input.value = text; input.listeners.get("input")(); };
const notice = (page) => page.main.querySelectorAll("p").find((el) => el.dataset.field === "notice");

for (const spec of PAGES) {
  for (const [status, text] of spec.refused) {
    test(`${spec.name}: a refused action (${status}) says why, in the card, with its controls back on`, async (t) => {
      const page = open(t, spec, (url) => {
        assert.equal(url, spec.write);
        return response({ detail: "the daemon broke" }, status);
      });
      await page.advance(0);
      type(page.match(), "123456");
      const submit = page.main.querySelectorAll("button").find((el) => el.dataset.action === spec.action);
      assert.equal(submit.disabled, false);
      await submit.listeners.get("click")();
      await page.advance(0);

      const shown = notice(page);
      assert.equal(shown.textContent, text);
      assert.equal(shown.className, "notice bad");
      assert.equal(shown.hidden, false);
      assert.ok(shown.closest(spec.card));
      assert.equal(document.activeElement, shown, "the refusal takes focus so it is read");
      const controls = page.main.querySelectorAll("button,input");
      assert.ok(controls.length > 2);
      assert.deepEqual(controls.filter((el) => el.disabled), []);
      assert.equal(page.match().value, "123456", "the typed code is still there to correct");
    });
  }

  test(`${spec.name}: focus and caret in a draft survive a poll that changes the list`, async (t) => {
    const page = open(t, spec);
    await page.advance(0);
    const input = page.match();
    type(input, "123456");
    input.focus();
    input.setSelectionRange(3, 3);

    page.state.extra = ["another"];
    await page.advance(5000);
    assert.match(page.main.textContent, /another/);
    const drawn = page.match();
    assert.notEqual(drawn, input, "the poll rebuilt the card");
    assert.equal(document.activeElement, drawn);
    assert.equal(drawn.selectionStart, 3);
    assert.equal(drawn.value, "123456");
  });

  test(`${spec.name}: a poll that changes nothing does not rebuild the page`, async (t) => {
    const page = open(t, spec);
    await page.advance(0);
    const input = page.match();
    await page.advance(5000);
    assert.equal(page.match(), input);
  });

  test(`${spec.name}: a countdown runs on the daemon's clock, not the browser's`, async (t) => {
    const page = open(t, spec);
    await page.advance(0);
    assert.match(page.main.textContent, /(10:00|9:59) left/);
  });

  test(`${spec.name}: teardown leaves no interval or timeout`, async (t) => {
    const page = open(t, spec);
    await page.advance(0);
    assert.equal(page.intervals.size, 1);
    assert.equal(page.timers.size, 1);
    page.stop();
    assert.equal(page.intervals.size, 0);
    assert.equal(page.timers.size, 0);
  });
}

test("Marketplace: a poll keeps focus inside the open consent, on the same node", async (t) => {
  const marketplace = PAGES[1];
  const page = open(t, marketplace);
  await page.advance(0);
  page.main.querySelectorAll("button").find((el) => el.dataset.action === "setup").listeners.get("click")();
  const heading = page.main.querySelectorAll("h3").find((el) => el.textContent.startsWith("Set up app"));
  const cancel = page.main.querySelectorAll("button").find((el) => el.textContent === "Cancel");
  cancel.focus();
  page.state.extra = ["another"];
  await page.advance(5000);
  assert.match(page.main.textContent, /another/);
  assert.equal(document.activeElement, cancel);
  assert.ok(page.main.contains(heading));
});
