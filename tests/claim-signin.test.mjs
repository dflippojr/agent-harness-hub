import assert from "node:assert/strict";
import test from "node:test";
import { renderOverview } from "../harness_hub/static/pages/overview.mjs";
import { INVALID_LINK, renderSignin, sentences } from "../harness_hub/static/pages/signin.mjs";
import { mount, response, settle } from "./page-fake.mjs";

function* walk(el) {
  yield el;
  for (const child of el.children || []) yield* walk(child);
}

const find = (main, match) => [...walk(main)].find((el) => el.attrs && match(el));
const byClass = (main, name) => [...walk(main)].filter((el) => (el.className || "").split(" ").includes(name));

const overview = (claim, extra = {}) => ({
  reachable: true, restarting: false, error: "", fetched_at: Date.now() / 1000,
  hub: { version: "0.0.0", daemon_url: "http://127.0.0.1:8085" },
  daemon: {}, health: { ok: true }, capabilities: {}, ownership: null, claim, ...extra,
});

const pending = (secondsLeft) => ({
  state: "pending", message: "Waiting for the owner to approve this claim on the daemon host.",
  request_id: "pr-claim1", match_code: "987887", expires_at: Date.now() / 1000 + secondsLeft,
  approve_command: "harness hub approve pr-claim1 --match 987887", release_command: "harness hub release --confirm",
});

async function draw(t, data) {
  const page = mount(t, renderOverview, () => Promise.resolve(response(data)));
  await settle();
  return page;
}

test("a pending claim shows step 2 of 3, the boxed match code and a live countdown", async (t) => {
  const page = await draw(t, overview(pending(300)));
  const steps = find(page.main, (el) => el.attrs.get("aria-label") === "Claim steps");
  assert.equal(steps.tag, "ol");
  assert.deepEqual(steps.children.map((li) => li.attrs.get("aria-current")), [undefined, "step", undefined]);
  assert.match(steps.children[0].textContent, /^Claim started \(done\)$/);
  const code = find(page.main, (el) => el.attrs.get("role") === "img");
  assert.equal(code.attrs.get("aria-label"), "Match code 9 8 7 8 8 7");
  assert.deepEqual(code.children.map((digit) => digit.textContent), ["9", "8", "7", "8", "8", "7"]);
  const [clock] = byClass(page.main, "claim-clock");
  assert.match(clock.textContent, /^[45]:\d\d left$/);
  assert.match(page.main.textContent, /deny it there with harness hub deny pr-claim1\./);

  // One timer redraws the chip each second; a poll's redraw replaces it, and leaving the page stops it.
  assert.equal(page.intervals.size, 1);
  const [interval] = page.intervals.values();
  assert.equal(interval.delay, 1000);
  clock.children = [];
  interval.callback();
  assert.match(clock.textContent, /^[45]:\d\d left$/);
  const [id, poll] = [...page.timers][0];
  page.timers.delete(id);
  await poll.callback();
  await settle();
  assert.equal(page.intervals.size, 1);
  page.stop();
  assert.equal(page.intervals.size, 0);
});

test("a claim past its expiry reads Expired", async (t) => {
  const page = await draw(t, overview(pending(-5)));
  const [clock] = byClass(page.main, "claim-clock");
  assert.equal(clock.textContent, "Expired");
});

test("Stop waiting is a quiet text button", async (t) => {
  const page = await draw(t, overview(pending(300)));
  const button = find(page.main, (el) => el.tag === "button" && el.textContent === "Stop waiting");
  assert.equal(button.className, "btn text");
});

for (const [name, extra, disabled] of [
  ["answering", {}, false],
  ["not answering", { reachable: false }, true],
  ["answering with an error", { reachable: false, answered: true }, false],
]) {
  test(`Start claim with the daemon ${name} is ${disabled ? "disabled with the reason" : "enabled"}`, async (t) => {
    const page = await draw(t, overview({ state: "unclaimed", message: "This Hub has not claimed the daemon yet." }, extra));
    const button = find(page.main, (el) => el.tag === "button" && el.textContent === "Start claim");
    assert.equal(button.attrs.has("disabled"), disabled);
    assert.equal(page.intervals.size, 0);
    if (disabled) {
      assert.equal(button.attrs.get("aria-describedby"), "claim-start-why");
      const why = find(page.main, (el) => el.attrs.get("id") === "claim-start-why");
      assert.equal(why.textContent, "The daemon is not answering, so a claim cannot start yet.");
    }
    const steps = find(page.main, (el) => el.attrs.get("aria-label") === "Claim steps");
    assert.deepEqual(steps.children.map((li) => li.attrs.get("aria-current")), ["step", undefined, undefined]);
    assert.ok(find(page.main, (el) => el.attrs.get("id") === "status-title").textContent === "Before you claim");
  });
}

test("the server's refusals read as sentences", () => {
  assert.deepEqual(sentences(INVALID_LINK),
    { title: "That sign-in link is not valid.", detail: "Links work once, for ten minutes." });
  assert.deepEqual(sentences("too many sign-in attempts; wait a minute"),
    { title: "Too many sign-in attempts.", detail: "Wait a minute." });
  assert.deepEqual(sentences("sign in first"), { title: "Sign in first.", detail: "" });
  assert.deepEqual(sentences("The Hub server did not answer."), { title: "The Hub server did not answer.", detail: "" });
});

for (const [source, message] of [["the page's own check", INVALID_LINK],
  ["the server's refusal, as api.mjs words it", "That sign-in link is not valid; links work once, for ten minutes."]]) {
  test(`the sign-in card numbers its steps and shows a used link (${source}) as a sentence with what to do`, async (t) => {
    let main;
    mount(t, (el) => {
      main = el;
      renderSignin(el, { session: { signin_command: "python -m harness_hub signin-link" }, error: new Error(message) });
      return () => {};
    }, () => Promise.reject(new Error("no calls")));
    const card = find(main, (el) => el.attrs.get("aria-labelledby") === "signin-title");
    assert.equal(find(card, (el) => el.tag === "h1").textContent, "Sign in to the Hub");
    const alert = find(card, (el) => el.attrs.get("role") === "alert");
    assert.equal(alert.textContent, "That sign-in link is not valid.Links work once, for ten minutes. Make a new one below.");
    const steps = byClass(card, "signin-step");
    assert.equal(steps.length, 2);
    assert.match(steps[0].textContent, /^1On the daemon host.*python -m harness_hub signin-link/);
    const docker = find(card, (el) => el.tag === "details");
    assert.match(docker.textContent, /^The Hub runs in Docker/);
    assert.equal(find(main, (el) => el.attrs.get("data-action") === "tailnet-signin"), undefined);
  });
}
