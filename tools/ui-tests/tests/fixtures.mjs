// Shared fixtures: a page signed in to the run's Hub, page readiness, overflow and console checks.
import { createHash, randomBytes } from "node:crypto";
import { test as base, expect } from "@playwright/test";
import { signinLink } from "../instance.mjs";

export { expect };

export const SECTIONS = [
  { id: "overview", label: "Overview", ready: (page) => page.getByRole("heading", { name: "Daemon", exact: true }) },
  { id: "settings", label: "Settings", ready: (page) => page.getByRole("spinbutton", { name: "Maximum turns" }) },
  { id: "modules", label: "Modules", ready: (page) => page.getByRole("heading", { name: /^Installed/ }) },
  { id: "apps", label: "Apps", ready: (page) => page.getByRole("heading", { name: "Paired apps and keys" }) },
  { id: "spend", label: "Spend", ready: (page) => page.getByRole("table", { name: "App usage, sortable by any column" }) },
  { id: "pairing", label: "Pairing requests", ready: (page) => page.getByRole("heading", { name: /^History · last day/ }) },
  { id: "marketplace", label: "Marketplace", ready: (page) => page.getByRole("button", { name: "Set up financial-planner" }) },
  { id: "admin", label: "Accounts and maintenance", ready: (page) => page.getByRole("heading", { name: "Synthetic member" }) },
];

export const hubUrl = () => process.env.HUB_UI_URL;
export const daemonUrl = () => process.env.HUB_UI_DAEMON_URL;

// Each tab needs its own one-time link; its code travels in the fragment and the page swaps it for #/overview.
export async function signIn(page, dir = process.env.HUB_UI_DIR) {
  // A fresh document: on a page already showing the Hub, a fragment-only goto would not boot the app again.
  await page.goto("about:blank");
  await page.goto(signinLink(dir));
  await expect(page).toHaveURL(/#\/overview$/);
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
}

export async function openSection(page, id) {
  const section = SECTIONS.find((s) => s.id === id);
  await page.goto(`${hubUrl()}/#/${id}`);
  await expect(page.getByRole("heading", { name: section.label, level: 1 })).toBeVisible();
  await expect(section.ready(page).first()).toBeVisible();
}

export async function expectNoHorizontalOverflow(page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

export const isPhone = (testInfo) => testInfo.project.name.startsWith("phone") || testInfo.project.name.startsWith("pixel");
export const scheme = (testInfo) => testInfo.project.use.colorScheme;
// Functional journeys repeat the same server round trips against one shared instance, so they run in one project per
// engine and viewport: desktop-light (Chromium) and phone-dark (WebKit). navigation.spec.mjs covers every scheme.
const FUNCTIONAL_PROJECTS = ["desktop-light", "phone-dark"];
export const functionalProject = (testInfo) => test.skip(!FUNCTIONAL_PROJECTS.includes(testInfo.project.name),
  "a functional journey: desktop-light (Chromium) and phone-dark (WebKit) cover the engine and viewport; navigation.spec.mjs covers every scheme");
// Unique per project and run, for entities that tests create on the shared instance.
export const unique = (testInfo, label) => `${label} ${testInfo.project.name} ${Date.now().toString(36)}`;

// The daemon's public pairing call, as an App makes it; returns the request id and the code the App would show.
export async function appAsksToPair(request, name, scopes = ["sessions"]) {
  const asked = await request.post(`${daemonUrl()}/api/v1/pair/requests`, {
    data: { name, scopes, code_challenge: "A".repeat(43), code_challenge_method: "S256" } });
  expect(asked.status()).toBe(201);
  return asked.json();
}

// An App's PKCE pair: it claims with the challenge and later redeems its own token with the verifier.
export function pkce() {
  const verifier = randomBytes(32).toString("base64url");
  return { verifier, challenge: createHash("sha256").update(verifier).digest("base64url") };
}

// The Hub's own API from a signed-in tab, with the headers its pages send; for setting up state the journey is not about.
export async function hubApi(page, path, { method = "GET", body } = {}) {
  return page.evaluate(async ([path, method, body]) => {
    const headers = { Accept: "application/json", "X-Hub-Session": sessionStorage.getItem("hub.session") };
    if (method !== "GET") Object.assign(headers, { "X-Hub-Request": "1", "Content-Type": "application/json" });
    const response = await fetch(`/api/${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, json: await response.json().catch(() => null) };
  }, [path, method, body]);
}

// Arm a slot for a Marketplace entry as Set up and Approve would; slots are owner-made, so the daemon's per-source
// request limit does not apply. Returns the slot.
export async function armSlot(page, name = "Agent Harness Web") {
  const { json: view } = await hubApi(page, "marketplace");
  const entry = view.entries.find((e) => e.name === name);
  const armed = await hubApi(page, "marketplace/arm", { method: "POST", body: {
    catalog_app_id: entry.catalog_app_id, scopes: entry.scopes.map((s) => s.scope), consent_digest: entry.consent_digest } });
  expect(armed.status).toBe(201);
  return armed.json.request;
}

// The App claims an armed slot with its challenge; returns the daemon's answer, which holds the code the App shows.
export async function appClaims(request, id, challenge) {
  const claimed = await request.post(`${daemonUrl()}/api/v1/pair/requests/${id}/claim`, {
    data: { code_challenge: challenge, code_challenge_method: "S256" } });
  expect(claimed.status()).toBe(200);
  return claimed.json();
}

// The App fetches its own token once the owner approved; returns the new key's inventory row.
export async function appRedeems(request, id, verifier) {
  const redeemed = await request.post(`${daemonUrl()}/api/v1/pair/requests/${id}/token`, { data: { code_verifier: verifier } });
  expect(redeemed.status()).toBe(201);
  return (await redeemed.json()).app;
}

// A paired App with its own key: arm, claim, confirm and redeem, all outside the page.
export async function pairApp(page, request, name) {
  const { verifier, challenge } = pkce();
  const slot = await armSlot(page, name);
  const { match_code } = await appClaims(request, slot.id, challenge);
  expect((await hubApi(page, `pairing/${slot.id}/confirm`, { method: "POST", body: { match: match_code } })).status).toBe(200);
  return appRedeems(request, slot.id, verifier);
}

// The JSON bodies of the page's writes to one Hub API path, as they leave the browser.
export function writesTo(page, pathname) {
  const bodies = [];
  page.on("request", (r) => {
    if (r.method() !== "GET" && new URL(r.url()).pathname === pathname) bodies.push(r.postDataJSON());
  });
  return bodies;
}

// HUB-04's save and rollback of Maximum turns, shared by settings.spec.mjs and the verify skill's drive step. The save
// bar reports Validate and Save; the notice under the page head reports Roll back and Restart.
export const settingsControls = (page) => ({
  turns: page.getByRole("spinbutton", { name: "Maximum turns" }),
  saveBar: page.getByRole("region", { name: "Settings changes" }),
  status: page.locator("main > p.notice[aria-live]"),
});

export async function saveMaxTurns(page, from, to) {
  const { turns, saveBar } = settingsControls(page);
  await turns.fill(String(to));
  await expect(saveBar).toContainText(`1 unsaved change · sessions.max_turns ${from} → ${to}`);
  await saveBar.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(saveBar).toContainText(`sessions.max_turns: ${from} → ${to}`);
  await expect(saveBar).toContainText("Validated.");
  await saveBar.getByRole("button", { name: "Save", exact: true }).click();
  await expect(saveBar).toContainText("Saved.");
  await expect(saveBar.getByRole("button", { name: "Save", exact: true })).toBeHidden();
  await page.reload();
  await expect(turns).toHaveValue(String(to));
}

export async function confirmRollback(page, to) {
  const { turns, status } = settingsControls(page);
  await page.getByRole("button", { name: "Roll back", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "Roll back settings?" })
    .getByRole("button", { name: "Confirm rollback" }).click();
  await expect(status).toContainText("Rolled back to revision");
  await page.reload();
  await expect(turns).toHaveValue(String(to));
}

export const test = base.extend({
  // Page errors and console errors, apart from the known 404 the Settings page logs for an absent memory module
  // (#41) and WebKit's report of a fetch that a reload cancelled in the old document ("... due to access control
  // checks."). Tests assert the list is empty at the end.
  errors: async ({ page }, use) => {
    const errors = [];
    const cancelled = / due to access control checks\.$/;
    page.on("pageerror", (error) => { if (!cancelled.test(error.message)) errors.push(error.message); });
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      if (message.location().url.endsWith("/api/settings/memory")) return;
      if (cancelled.test(message.text())) return;
      errors.push(message.text());
    });
    await use(errors);
  },
  signedIn: async ({ page }, use) => {
    await signIn(page);
    await use(page);
  },
});
