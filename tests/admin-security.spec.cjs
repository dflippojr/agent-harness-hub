// Synthetic intercepted Hub responses: no server or real account is used.
const { test, expect } = require("playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const op = (id, group, label, method = "GET", targets = [], fields = [], confirmation = "") =>
  ({ id, group, label, method, targets, fields, confirmation, cli: "harness synthetic " + id });
const field = (name, type = "text", required = false) => ({ name, type, required });
const catalog = [op("accounts", "accounts", "List members"), op("account", "accounts", "Member detail", "GET", ["user_id"]),
  op("account_save", "accounts", "Update member", "PATCH", ["user_id"], [field("enabled", "boolean")], "Disabling cancels work."),
  op("github_reset", "accounts", "Erase GitHub credential", "POST", ["user_id"], [], "Erase credential?"),
  op("google_invite", "accounts", "Create Google link code", "POST", ["user_id"]),
  op("google_cancel", "accounts", "Cancel Google link code", "DELETE", ["user_id"], [], "Cancel code?"),
  op("google_revoke", "accounts", "Revoke Google sessions", "POST", ["user_id"], [], "End sessions?"),
  op("google_unlink", "accounts", "Unlink Google", "DELETE", ["user_id"], [], "Unlink?"),
  op("credentials", "credentials", "List provider assignments"), op("credential_revoke", "credentials", "Revoke provider assignment", "DELETE", ["credential_id"], [], "Revoke?"),
  op("skills", "skills", "Skills and proposals"), op("proposal", "skills", "Read proposal", "GET", ["pid"]),
  op("skill_install", "skills", "Install reviewed proposal", "POST", ["pid"], [field("content_hash", "hash", true)], "Install reviewed hash?"),
  op("skill_reject", "skills", "Reject proposal", "POST", ["pid"]), op("skill_reopen", "skills", "Reopen proposal", "POST", ["pid"]),
  op("skill_review", "skills", "Run hosted review", "POST", ["pid"]), op("proposal_delete", "skills", "Delete proposal", "DELETE", ["pid"], [], "Delete?"),
  op("maintenance", "maintenance", "Maintenance status"), op("cleanup", "maintenance", "Clean up", "POST", [], [], "Erase old workspaces?"),
  op("retention_preview", "maintenance", "Preview image archive retention", "POST"),
  op("retention_apply", "maintenance", "Apply image archive retention", "POST", [], [field("confirmation", "text", true)], "Erase preview images?"),
  op("runners", "runners", "List runners"), op("runner_update", "runners", "Update runner", "POST", ["name"], [], "Update?"),
  op("audit", "audit", "Read retained audit", "GET", [], [field("action"), field("before_id", "integer")])];
const member = { user_id: "u-synthetic", display_name: "Synthetic member", enabled: true, google: { linked: false } };
const proposal = { id: "p-synthetic", title: "Synthetic proposal", content_hash: "a".repeat(64), skill_md: "<img src=x onerror=alert(1)>",
  slug: "synthetic", purpose: "Synthetic purpose", activation_suggestion: "Synthetic activation",
  source_session_id: "s-synthetic", review_complete: true,
  examples: [{ prompt: "<script>example prompt</script>", expected: "<img src=x onerror=alert(1)>expected text" }],
  references: [{ path: "notes.txt", content: "<script>fixture</script>" }] };

async function mount(page, { failure = false, overrides = {} } = {}) {
  const writes = [], reads = [], errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://hub.test/**", async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    if (pathname.startsWith("/api/")) {
      const id = pathname.split("/").pop();
      if (request.method() !== "GET") writes.push({ id, method: request.method(), body: request.postDataJSON(), target: Object.fromEntries(url.searchParams) });
      else reads.push(url);
      if (id === "catalog") return route.fulfill({ json: { operations: catalog, not_offered: [
        { label: "Owner keys", reason: "Hub key cannot mint owner keys", cli: "harness keys create" }] } });
      if (failure && id === "cleanup") return route.fulfill({ status: 503, json: { detail: "Daemon unavailable; inspect state." } });
      const replies = { accounts: [member], account: member, account_save: { ...member, enabled: false }, credentials: [],
        skills: { enabled: true, proposals: [proposal], installed: [] }, proposal, skill_install: {}, maintenance: { free_gb: 10 },
        retention_preview: { count: 2, bytes: 1024, confirmation: "DELETE 2 synthetic" }, retention_apply: { removed: 2 },
        runners: [], google_invite: { code: "ahg-synthetic-once", expires_at: 4102444800 },
        audit: { items: [{ id: 2, action: "create" }], next_before_id: url.searchParams.has("before_id") ? null : 2 } };
      return route.fulfill({ json: overrides[id] ?? replies[id] ?? { ok: true } });
    }
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body:
      '<link rel="stylesheet" href="/static/style.css"><main id="main"></main><script type="module">import {renderAdmin} from "/static/pages/admin.mjs"; window.stopAdmin = renderAdmin(document.querySelector("main"));</script>' });
    return route.fulfill({ contentType: pathname.endsWith(".css") ? "text/css" : "text/javascript", body:
      await readFile(path.join(__dirname, "../harness_hub", pathname), "utf8") });
  });
  await page.goto("http://hub.test/");
  await expect(page.getByRole("heading", { name: "Synthetic member" })).toBeVisible();
  return { writes, reads, errors };
}
const form = page => page.locator(".admin-form");
const select = (page, id) => page.getByLabel("Choose action", { exact: true }).selectOption(id);

test("disable and cleanup require explicit confirmation; cancel sends nothing", async ({ page }) => {
  const { writes, errors } = await mount(page, { failure: true });
  await select(page, "account_save"); await form(page).getByLabel("Member id").fill("u-synthetic");
  await form(page).getByLabel("Member enabled").selectOption("false"); await form(page).getByRole("button").click();
  await expect(page.getByRole("region", { name: "Confirm action" })).toContainText("u-synthetic"); expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click(); expect(writes).toHaveLength(0);
  await form(page).getByRole("button").click(); await page.getByRole("button", { name: "Confirm update member", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ id: "account_save", method: "PATCH", target: { user_id: "u-synthetic" }, body: { enabled: false, confirm: true } });
  await expect(form(page).getByRole("button")).toBeEnabled();
  await page.getByRole("button", { name: "Maintenance", exact: true }).click(); await expect(page.getByRole("status")).toHaveText("Current daemon state.");
  await select(page, "cleanup"); await form(page).getByRole("button").click();
  await page.getByRole("button", { name: "Confirm clean up" }).click();
  await expect(page.getByRole("status")).toContainText("Daemon unavailable");
  expect(writes.filter(write => write.id === "cleanup")).toHaveLength(1); expect(errors).toEqual([]);
});

test("Google code is shown once and is cleared by action navigation and disposal", async ({ page }) => {
  const { writes, errors } = await mount(page);
  await select(page, "google_invite"); await form(page).getByLabel("Member id").fill("u-synthetic");
  await form(page).getByRole("button").click(); await expect(page.locator("[data-link-code]")).toHaveText("ahg-synthetic-once");
  await expect(form(page).getByRole("button")).toBeEnabled(); await select(page, "account");
  await expect(page.getByText("ahg-synthetic-once")).toHaveCount(0);
  await select(page, "google_invite"); await form(page).getByLabel("Member id").fill("u-synthetic");
  await form(page).getByRole("button").click(); await expect(page.locator("[data-link-code]")).toBeVisible();
  await page.evaluate(() => window.stopAdmin()); await expect(page.getByText("ahg-synthetic-once")).toHaveCount(0);
  expect(writes).toHaveLength(2); expect(errors).toEqual([]);
});

test("proposal markup stays text and installation uses the reviewed hash", async ({ page }) => {
  const { writes, errors } = await mount(page);
  await page.getByRole("button", { name: "Skills", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Synthetic proposal" })).toBeVisible();
  await page.locator(".admin-row").getByRole("button", { name: "Read proposal" }).click();
  await form(page).getByRole("button").click(); await expect(page.locator(".admin-output")).toContainText("<img src=x onerror=alert(1)>");
  expect(await page.locator("img, .admin-output script").count()).toBe(0);
  for (const text of [proposal.slug, proposal.purpose, proposal.activation_suggestion, proposal.source_session_id,
    proposal.examples[0].prompt, proposal.examples[0].expected, proposal.references[0].path, proposal.references[0].content])
    await expect(page.locator(".admin-output")).toContainText(text);
  await expect(form(page).getByRole("button")).toBeEnabled();
  await page.locator(".admin-output").getByRole("button", { name: "Install reviewed proposal" }).click();
  await expect(form(page).getByLabel("Exact reviewed content hash")).toHaveValue("a".repeat(64));
  await form(page).getByRole("button").click(); expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Confirm install reviewed proposal" }).click();
  await expect.poll(() => writes.length).toBe(1); expect(writes[0].body).toEqual({ content_hash: "a".repeat(64), confirm: true });
  expect(errors).toEqual([]);
});

test("retention needs preview and an owner-typed exact phrase", async ({ page }) => {
  const { writes, errors } = await mount(page);
  await page.getByRole("button", { name: "Maintenance", exact: true }).click(); await expect(page.getByRole("status")).toHaveText("Current daemon state.");
  await select(page, "retention_apply"); await form(page).getByLabel("Type the phrase", { exact: false }).fill("DELETE 2 synthetic");
  await form(page).getByRole("button").click(); await expect(page.getByRole("status")).toContainText("fresh retention preview"); expect(writes).toHaveLength(0);
  await select(page, "retention_preview"); await form(page).getByRole("button").click(); await expect(page.getByRole("button", { name: "Apply preview" })).toBeVisible();
  await expect(form(page).getByRole("button")).toBeEnabled(); await page.getByRole("button", { name: "Apply preview" }).click();
  await expect(form(page).getByLabel("Type the phrase", { exact: false })).toHaveValue("");
  await form(page).getByLabel("Type the phrase", { exact: false }).fill("DELETE 2 synthetic");
  await form(page).getByRole("button").click(); await page.getByRole("button", { name: "Confirm apply image archive retention" }).click();
  await expect.poll(() => writes.length).toBe(2); expect(writes[1].body).toEqual({ confirmation: "DELETE 2 synthetic", confirm: true });
  expect(errors).toEqual([]);
});

for (const width of [390, 1280]) test("layout and audit paging at " + width, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 }); const { reads, errors } = await mount(page);
  await page.getByRole("button", { name: "Audit", exact: true }).click(); await expect(page.getByRole("button", { name: "Next audit page" })).toBeVisible();
  await expect(form(page).getByRole("button")).toBeEnabled();
  await page.getByRole("button", { name: "Next audit page" }).click();
  await expect.poll(() => reads.some(url => url.searchParams.get("before_id") === "2")).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const heights = await page.locator("button, input, select, textarea").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().height));
  expect(heights.every(height => height >= 44)).toBe(true); expect(errors).toEqual([]);
});


test("incomplete review blocks both the install shortcut and a manually selected install", async ({ page }) => {
  const { writes } = await mount(page, { overrides: { proposal: { ...proposal, review_complete: false } } });
  await page.getByRole("button", { name: "Skills", exact: true }).click();
  await select(page, "proposal"); await form(page).getByLabel("Proposal id").fill(proposal.id);
  await form(page).getByRole("button").click();
  await expect(page.locator(".admin-output")).toContainText("review on the host CLI");
  await expect(page.locator(".admin-output").getByRole("button", { name: "Install reviewed proposal" })).toHaveCount(0);
  await select(page, "skill_install"); await form(page).getByLabel("Proposal id").fill(proposal.id);
  await form(page).getByLabel("Exact reviewed content hash").fill(proposal.content_hash);
  await form(page).getByRole("button").click();
  await expect(page.getByRole("status")).toContainText("Read the complete proposal");
  expect(writes).toHaveLength(0);
});

test("invalid targets lose only their own actions and safe rows remain actionable", async ({ page }) => {
  await mount(page, { overrides: { skills: { proposals: [{ id: "", title: "[withheld: token-shaped]" }, proposal], installed: [] } } });
  await page.getByRole("button", { name: "Skills", exact: true }).click();
  const bad = page.locator(".admin-row").filter({ has: page.getByRole("heading", { name: "[withheld: token-shaped]", exact: true }) });
  await expect(bad.getByRole("button")).toHaveCount(0);
  const good = page.locator(".admin-row").filter({ has: page.getByRole("heading", { name: "Synthetic proposal" }) });
  await expect(good.getByRole("button", { name: "Read proposal" })).toBeEnabled();
});
