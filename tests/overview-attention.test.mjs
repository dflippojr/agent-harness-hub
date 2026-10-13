import assert from "node:assert/strict";
import test from "node:test";
import { renderOverview } from "../harness_hub/static/pages/overview.mjs";
import { mount, response, settle } from "./page-fake.mjs";

const overview = (attention) => ({
  reachable: true, restarting: false, error: "", attention,
  hub: { version: "0.0.0", daemon_url: "http://127.0.0.1:8085" },
  daemon: {}, health: { ok: true }, capabilities: {}, ownership: null,
  claim: { state: "claimed", message: "claimed", key_id: "k-1", claimed_at: 1 },
});

function* walk(el) {
  yield el;
  for (const child of el.children || []) yield* walk(child);
}

async function card(t, attention) {
  const page = mount(t, renderOverview, () => Promise.resolve(response(overview(attention))));
  await settle();
  return [...walk(page.main)].find((el) => el.attrs && el.attrs.get("aria-labelledby") === "attention-title");
}

for (const [attention, text] of [
  [{ approve: 1, confirm: 1 }, "2 pairing requests need you: 1 to approve, 1 to confirm"],
  [{ approve: 1, confirm: 0 }, "1 pairing request needs you: 1 to approve"],
  [{ approve: 0, confirm: 3 }, "3 pairing requests need you: 3 to confirm"],
]) {
  test(`Overview shows "${text}" with a link to Pairing requests`, async (t) => {
    const shown = await card(t, attention);
    assert.ok(shown, "the Waiting for you card is drawn");
    assert.match(shown.textContent, /^Waiting for you/);
    assert.ok(shown.textContent.includes(text), shown.textContent);
    const link = [...walk(shown)].find((el) => el.tag === "a");
    assert.equal(link.attrs.get("href"), "#/pairing");
  });
}

for (const attention of [null, { approve: 0, confirm: 0 }, undefined]) {
  test(`Overview draws no Waiting for you card for attention ${JSON.stringify(attention)}`, async (t) => {
    assert.equal(await card(t, attention), undefined);
  });
}
