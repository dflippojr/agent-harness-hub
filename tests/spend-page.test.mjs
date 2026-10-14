import assert from "node:assert/strict";
import test from "node:test";
import { renderSpend } from "../harness_hub/static/pages/spend.mjs";
import { mount, response, settle } from "./page-fake.mjs";

const usage = (requests, cost) => ({ requests, prompt_tokens: requests * 10, completion_tokens: requests, cost_usd: cost });
const caps = (share, near) => ({ max_running: 2, max_queued: 10, running_used: 1, queued_used: 0, parked: 0, share, near });
const SPEND = {
  error: "", stale: false, fetched_at: 1, backends_error: "",
  apps: [
    { id: "k-cheap", name: "Cheap", kind: "app", state: "idle", usage: usage(1, 0.01), caps: caps(0.5, false) },
    { id: "k-busy", name: "Busy", kind: "app", state: "active", usage: usage(9, 2.5), caps: caps(1, true) },
    { id: "k-gone", name: "", kind: "app", state: "revoked", usage: usage(3, null), caps: null },
  ],
  backends: [
    { name: "claude", billing: "subscription", local: false, today: usage(2, 0.25), week: usage(5, 1) },
    { name: "llama", billing: "local", local: true, today: usage(4, null), week: usage(8, null) },
  ],
};

function* walk(el) {
  yield el;
  for (const child of el.children || []) yield* walk(child);
}
const rows = (page, attr) => [...walk(page.main)].filter((el) => el.attrs?.has(attr));

async function spend(t) {
  const page = mount(t, renderSpend, () => Promise.resolve(response(SPEND)));
  await settle();
  return page;
}

test("Spend sorts Apps by cost first, unknown cost last, and flags the App near its cap", async (t) => {
  const page = await spend(t);
  assert.deepEqual(rows(page, "data-app-id").map((el) => el.attrs.get("data-app-id")), ["k-busy", "k-cheap", "k-gone"]);
  const busy = rows(page, "data-app-id")[0];
  assert.equal(busy.className, "near");
  assert.ok(busy.textContent.includes("Near cap"), busy.textContent);
  assert.ok(page.main.textContent.includes("Near a session cap: Busy."), page.main.textContent);
  assert.ok(rows(page, "data-app-id")[2].textContent.includes("k-gone"));
});

test("Spend re-sorts on a header button and reverses on a second press", async (t) => {
  const page = await spend(t);
  const button = [...walk(page.main)].find((el) => el.attrs?.get("data-sort") === "name");
  button.listeners.get("click")();
  assert.deepEqual(rows(page, "data-app-id").map((el) => el.attrs.get("data-app-id")), ["k-busy", "k-cheap", "k-gone"]);
  const name = () => [...walk(page.main)].find((el) => el.attrs?.get("data-sort") === "name");
  name().listeners.get("click")();
  assert.deepEqual(rows(page, "data-app-id").map((el) => el.attrs.get("data-app-id")), ["k-gone", "k-cheap", "k-busy"]);
  const header = [...walk(page.main)].find((el) => el.tag === "th" && el.attrs.get("aria-sort") === "descending");
  assert.ok(header.textContent.startsWith("App"), header.textContent);
});

test("Spend shows a local backend's tokens but no cost", async (t) => {
  const page = await spend(t);
  const [hosted, local] = rows(page, "data-backend");
  assert.ok(hosted.textContent.includes("$0.25"), hosted.textContent);
  assert.ok(local.textContent.includes("Local model"));
  assert.ok(local.textContent.includes("44"), local.textContent);  // 4 requests: 40 prompt + 4 completion tokens
  assert.ok(!local.textContent.includes("$"), local.textContent);
});
