import assert from "node:assert/strict";
import test from "node:test";
import { renderModules } from "../harness_hub/static/pages/modules.mjs";
import { deferred, mount, response } from "./page-fake.mjs";

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
  const toggle = find(page.main, (el) => el.attrs.get("role") === "switch");
  assert.equal(toggle.checked, true);
  assert.ok(find(page.main, (el) => el.attrs.get("data-module") === "notifications"));
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
  const toggle = find(page.main, (el) => el.attrs.get("role") === "switch");
  failReads = true;
  toggle.checked = false;
  await toggle.listeners.get("change")();
  const notice = page.main.children[2];
  assert.ok(notice.textContent.startsWith("Saved notify.enabled=false. Revision 5."), notice.textContent);
  assert.ok(notice.textContent.includes("Switches unavailable: The daemon did not answer."), notice.textContent);
  assert.match(notice.className, /bad/);

  failReads = false;
  await page.advance(30000);
  assert.equal(notice.textContent, "Saved notify.enabled=false. Revision 5.");
  assert.equal(notice.className, "notice");
});

test("a switch moved while a refresh is under way goes back and saves nothing", async (t) => {
  let pending = null;
  const page = mount(t, renderModules, (url, options) => {
    if (url === "/api/modules" && pending) return pending.promise;
    if (url === "/api/settings/config" && options.method === "PATCH") throw new Error("no write expected");
    return Promise.resolve(url === "/api/modules" ? response(inventory) :
      url === "/api/settings/schema" ? response(schema) : response(config));
  });
  await page.advance(0);
  pending = deferred();
  // Refresh, not the poll: the fake clock would wait for the read this test holds open.
  const refresh = find(page.main, (el) => el.tag === "button" && el.textContent.includes("Refresh modules"));
  const refreshed = refresh.listeners.get("click")();
  await page.advance(0);
  const moved = find(page.main, (el) => el.attrs.get("role") === "switch");
  moved.checked = false;
  await moved.listeners.get("change")();
  const shown = find(page.main, (el) => el.attrs.get("role") === "switch");
  assert.notEqual(shown, moved);
  assert.equal(shown.checked, true);
  assert.equal(page.calls.filter((call) => call.method === "PATCH").length, 0);
  pending.resolve(response(inventory));
  pending = null;
  await refreshed;
  const after = find(page.main, (el) => el.attrs.get("role") === "switch");
  assert.equal(after.checked, true);
  // The refresh found nothing new, yet the switches drawn while it ran (disabled) are replaced by enabled ones.
  assert.equal(after.attrs.has("disabled"), false);
  await page.advance(30000);
  assert.equal(find(page.main, (el) => el.attrs.get("role") === "switch").attrs.has("disabled"), false);
});
