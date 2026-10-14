// Apps disables every control while an action runs; none may stay disabled once the page has reloaded.
import assert from "node:assert/strict";
import test from "node:test";
import { renderApps } from "../harness_hub/static/pages/apps.mjs";
import { Element, deferred, mount, response, settle } from "./page-fake.mjs";

const row = { id: "k-one", name: "Synthetic one", kind: "app", role: "app", state: "active", revoked_at: null,
  scopes: "sessions", origins: ["https://app.test"], created_at: 1, last_used_at: null, token_age_seconds: null,
  store: { sessions: {}, usage: {}, errors: 0, last_error: "" } };

function controls(el) {
  return ["button", "input"].includes(el.tag) ? [el, ...el.children.flatMap(controls)] : el.children.flatMap(controls);
}
const press = (el) => el.listeners.get("click")();
const labelled = (page, label) => controls(page.main).find((el) => el.textContent === label);

for (const failure of [false, true]) {
  test("Apps re-enables its header and Host commands controls after an action " + (failure ? "is refused" : "succeeds"), async (t) => {
    const revoke = deferred();
    Element.prototype.scrollIntoView = () => {};
    t.after(() => { delete Element.prototype.scrollIntoView; });
    const page = mount(t, renderApps, (url, options) => {
      if (options.method === "DELETE") return revoke.promise;
      if (url.endsWith("/limits")) return Promise.resolve(response({ configured: { max_running: null, max_queued: null },
        effective: { max_running: 2, max_queued: 4 }, running: 0, queued: 0, parked: 0, retention_days: null }));
      return Promise.resolve(url === "/api/apps" ?
        response({ state: "ready", apps: [row], entries: [] }) : response([]));
    });
    await settle();
    page.main.querySelectorAll = () => controls(page.main);
    press(labelled(page, "Open Synthetic one"));
    await settle();
    await settle();
    assert.ok(controls(page.main).every((el) => !el.disabled));

    // Revoke asks inside the danger zone, which redraws the detail; the toggles are those on screen with the question.
    press(labelled(page, "Revoke Synthetic one"));
    const toggles = controls(page.main).filter((el) => ["Host command", "Host commands"].includes(el.textContent));
    assert.ok(toggles.length >= 4, "the page head, caps, retention and the danger zone each have a Host commands button");
    press(labelled(page, "Confirm revoke Synthetic one"));
    await settle();
    assert.ok(toggles.every((el) => el.disabled), "disabled while the action runs");

    if (failure) revoke.resolve(response({ detail: "refused" }, 409));
    else revoke.resolve(response({}));
    await settle();
    await settle();
    assert.deepEqual(controls(page.main).filter((el) => el.disabled).map((el) => el.textContent), []);
    assert.ok(toggles.slice(0, 1).every((el) => !el.disabled));
  });
}
