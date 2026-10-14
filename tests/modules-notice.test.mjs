import assert from "node:assert/strict";
import test from "node:test";
import { renderModules } from "../harness_hub/static/pages/modules.mjs";
import { mount, response } from "./page-fake.mjs";

const inventory = { state: "ready", error: "", stale: false, fetched_at: 1, modules: [{ name: "notifications", state: "present" }] };
const schema = { settings: [{ key: "notify.enabled", type: "bool", modules: ["notifications"], writable: true, available: true }] };
const config = { revision: 4, restart_required: false, settings: [{ key: "notify.enabled", effective: true }] };
const down = () => response({ detail: "The daemon did not answer." }, 503);

function find(el, match) {
  if (match(el)) return el;
  for (const child of el.children) {
    const found = find(child, match);
    if (found) return found;
  }
  return null;
}

test("the switches notice clears once the settings reads recover", async (t) => {
  let failSchema = true;
  const page = mount(t, renderModules, (url) => Promise.resolve(
    url === "/api/modules" ? response(inventory) :
    url === "/api/settings/schema" ? (failSchema ? down() : response(schema)) :
    url === "/api/settings/config" ? response(config) : response({ detail: "nope" }, 404)));
  await page.advance(0);
  assert.equal(page.main.children[2].textContent, "Switches unavailable: The daemon did not answer.");
  assert.match(page.main.children[2].className, /bad/);
  failSchema = false;
  await page.advance(30000);
  assert.ok(page.main.children[5].textContent.includes("Switch notifications off"));
  assert.equal(page.main.children[2].textContent, "");
  assert.equal(page.main.children[2].className, "notice");
});

test("a saved switch stays confirmed when the follow-up settings reads fail", async (t) => {
  let failReads = false;
  const page = mount(t, renderModules, (url, options) => Promise.resolve(
    url === "/api/modules" ? response(inventory) :
    url === "/api/settings/schema" ? (failReads ? down() : response(schema)) :
    url === "/api/settings/config" && options.method === "PATCH" ? response({ revision: 5, restart_required: false }) :
    url === "/api/settings/config" ? response(config) : response({ detail: "nope" }, 404)));
  await page.advance(0);
  const toggle = find(page.main, (el) => el.textContent === "Switch notifications off");
  failReads = true;
  await toggle.listeners.get("click")();
  const notice = page.main.children[2];
  assert.ok(notice.textContent.startsWith("Saved notify.enabled=false. Revision 5."), notice.textContent);
  assert.ok(notice.textContent.includes("Switches unavailable: The daemon did not answer."), notice.textContent);
  assert.match(notice.className, /bad/);

  failReads = false;
  await page.advance(30000);
  assert.equal(notice.textContent, "Saved notify.enabled=false. Revision 5.");
  assert.equal(notice.className, "notice");
});
