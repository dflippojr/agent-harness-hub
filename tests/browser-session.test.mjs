import assert from "node:assert/strict";
import test from "node:test";
// Included in the existing CI browser behavior entrypoint.
import "./apps-controls.test.mjs";
import "./dom-components.test.mjs";
import "./overview-attention.test.mjs";
import "./overview-polling.test.mjs";
import "./page-polling.test.mjs";
import "./shell-status.test.mjs";
import { api } from "../harness_hub/static/lib/api.mjs";
import { switchFor } from "../harness_hub/static/pages/modules.mjs";
import { signinFromHash } from "../harness_hub/static/pages/signin.mjs";

test("any #signin= fragment is a link; only a well-formed one carries a code to post", () => {
  const code = "A".repeat(20);
  assert.deepEqual(signinFromHash(""), { link: false, code: "" });
  assert.deepEqual(signinFromHash("#/overview"), { link: false, code: "" });
  assert.deepEqual(signinFromHash("#signin=" + code), { link: true, code });
  for (const bad of ["#signin=", "#signin=bogus-code", "#signin=" + code + ".", "#signin=" + code + ")",
    "#signin=" + "A".repeat(129), "#signin=" + code + "&x=1"]) {
    assert.deepEqual(signinFromHash(bad), { link: true, code: "" }, bad);
  }
});

test("module switches use schema relationships, reject unrelated and ambiguous bool settings", () => {
  const notification = { key: "notify.enabled", type: "bool", modules: ["notifications"] };
  const schema = { settings: [notification,
    { key: "notifications.debug.enabled", type: "bool", modules: ["notifications"] },
    { key: "notifications.interval", type: "int", modules: ["notifications"] },
    { key: "missing.enabled", type: "bool", modules: [] }] };
  assert.equal(switchFor("notifications", schema), notification);
  assert.equal(switchFor("missing", schema), null);
  assert.equal(switchFor("notify", schema), null);
  assert.equal(switchFor("notifications", null), null);
  schema.settings.push({ key: "another.enabled", type: "bool", modules: ["notifications"] });
  assert.equal(switchFor("notifications", schema), null);
});

test("origin binding survives reloads, accompanies API calls and ends at sign-out", async () => {
  const storage = new Map();
  const calls = [];
  globalThis.sessionStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, ...options });
    if (options.method === "POST") {
      return new Response(null, { status: 204, headers: { "X-Hub-Session": "origin-only-binding" } });
    }
    if (options.method === "DELETE") return new Response(null, { status: 204 });
    return new Response(JSON.stringify({ signed_in: true }), { status: 200 });
  };
  await api("session", { method: "POST", body: { code: "dummy-signin-code" } });
  assert.equal(storage.get("hub.session"), "origin-only-binding");
  await api("session");
  await api("overview");
  assert.equal(calls[1].headers["X-Hub-Session"], "origin-only-binding");
  assert.equal(calls[2].headers["X-Hub-Session"], "origin-only-binding");
  assert.equal(calls[2].credentials, "same-origin");
  assert.equal(calls[2].cache, "no-store");
  await api("session", { method: "DELETE" });
  assert.equal(calls[3].headers["X-Hub-Session"], "origin-only-binding");
  assert.equal(storage.has("hub.session"), false);
  await api("session");
  assert.equal(calls[4].headers["X-Hub-Session"], undefined);
});
