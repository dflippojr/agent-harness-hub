// Synthetic intercepted Hub responses: no server or real account is used.
const { test, expect } = require("playwright/test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const op = (id, group, label, method = "GET", targets = [], fields = [], confirmation = "") =>
  ({ id, group, label, method, targets, fields, confirmation, cli: "harness synthetic " + id });
const field = (name, type = "text", required = false) => ({ name, type, required });
const catalog = [op("accounts", "accounts", "List members"), op("account", "accounts", "Member detail", "GET", ["user_id"]),
  op("account_add", "accounts", "Add member", "POST", [], [field("login", "text", true), field("display_name", "text", true),
    field("disk_quota_bytes", "integer"), field("max_running", "integer"), field("max_queued", "integer")]),
  op("account_save", "accounts", "Update member", "PATCH", ["user_id"], [field("login"), field("display_name"), field("enabled", "boolean"),
    field("disk_quota_bytes", "integer"), field("max_running", "integer"), field("max_queued", "integer")], "Disabling cancels work."),
  op("github_reset", "accounts", "Erase GitHub credential", "POST", ["user_id"], [], "Erase credential?"),
  op("google_invite", "accounts", "Create Google link code", "POST", ["user_id"]),
  op("google_cancel", "accounts", "Cancel Google link code", "DELETE", ["user_id"], [], "Cancel code?"),
  op("google_revoke", "accounts", "Revoke Google sessions", "POST", ["user_id"], [], "End sessions?"),
  op("google_unlink", "accounts", "Unlink Google", "DELETE", ["user_id"], [], "Unlink?"),
  op("account_audit", "accounts", "Household audit", "GET", [], [field("limit", "integer")]),
  op("credentials", "credentials", "List provider assignments"), op("credential_revoke", "credentials", "Revoke provider assignment", "DELETE", ["credential_id"], [], "Revoke?"),
  op("skills", "skills", "Skills and proposals"), op("proposal", "skills", "Read proposal", "GET", ["pid"]),
  op("skill_install", "skills", "Install reviewed proposal", "POST", ["pid"], [field("content_hash", "hash", true)], "Install reviewed hash?"),
  op("skill_reject", "skills", "Reject proposal", "POST", ["pid"]), op("skill_reopen", "skills", "Reopen proposal", "POST", ["pid"]),
  op("skill_review", "skills", "Run hosted review", "POST", ["pid"]), op("proposal_delete", "skills", "Delete proposal", "DELETE", ["pid"], [], "Delete?"),
  op("skill_disable", "skills", "Disable skill", "POST", ["slug"]),
  op("skill_enable", "skills", "Enable skill", "POST", ["slug"]),
  op("skill_projects", "skills", "Set project allowlist", "PUT", ["slug"], [field("projects", "list", true)], "Added projects receive this skill automatically."),
  op("skill_rollback", "skills", "Roll back skill", "POST", ["slug"], [], "Roll back?"),
  op("skill_uninstall", "skills", "Uninstall skill", "POST", ["slug"], [], "Uninstall?"),
  op("skill_export", "skills", "Export skill", "GET", ["slug"]),
  op("remote", "runners", "Remote Control folders"),
  op("folder_trust", "runners", "Open folder trust prompt", "POST", ["project"], [], "Open trust?"),
  op("folder_forget", "runners", "Forget discovered folder", "DELETE", ["slug"], [], "Forget?"),
  op("scan", "runners", "Scan progress", "GET", ["scan_id"]),
  op("scan_cancel", "runners", "Cancel scan", "POST", ["scan_id"], [], "Cancel?"),
  op("scan_promote", "runners", "Add scanned folder", "POST", ["scan_id", "candidate_id"], [field("slug"), field("confirmed_path"), field("confirmed_markers", "list")], "Add folder?"),
  op("maintenance", "maintenance", "Maintenance status"), op("cleanup", "maintenance", "Clean up", "POST", [], [], "Erase old workspaces?"),
  op("retention_preview", "maintenance", "Preview image archive retention", "POST"),
  op("retention_apply", "maintenance", "Apply image archive retention", "POST", [], [field("confirmation", "text", true)], "Erase preview images?"),
  op("runners", "runners", "List runners"), op("runner_update", "runners", "Update runner", "POST", ["name"], [], "Update?"),
  op("audit", "audit", "Read retained audit", "GET", [], [field("action"), field("before_id", "integer")])];
const member = { user_id: "u-synthetic", login: "member@example.test", display_name: "Synthetic member", enabled: true,
  disk_quota_bytes: 20 * 1024 ** 3, disk_used_bytes: 0, max_running: 1, max_queued: 2, running: 0, queued: 0,
  created_at: 1760360280, updated_at: 1760360280, last_activity_at: null,
  google: { linked: false, email: "", active_web_sessions: 0, invitation_expires_at: null } };
const guest = { ...member, user_id: "u-guest", login: "guest@example.test", display_name: "Synthetic guest", enabled: false,
  disk_used_bytes: 312 * 1024 ** 2, google: { linked: true, email: "guest@example.test", active_web_sessions: 1 } };
const proposal = { id: "p-synthetic", title: "Synthetic proposal", content_hash: "a".repeat(64), skill_md: "<img src=x onerror=alert(1)>",
  slug: "synthetic", purpose: "Synthetic purpose", activation_suggestion: "Synthetic activation",
  source_session_id: "s-synthetic", review_complete: true,
  examples: [{ prompt: "<script>example prompt</script>", expected: "<img src=x onerror=alert(1)>expected text" }],
  references: [{ path: "notes.txt", content: "<script>fixture</script>" }] };

// Accounts follow tests/admin_fake.py: a member read answers for the member asked for, and a saved change stays.
// hold(id, method), when given, may return a promise the reply waits for (a slow daemon).
// refuse(id, method), when given, may return a message the reply refuses with (a daemon that stops answering).
async function mount(page, { failure = false, overrides = {}, hold = () => null, refuse = () => null } = {}) {
  const writes = [], reads = [], errors = [];
  const people = structuredClone(Array.isArray(overrides.accounts) ? overrides.accounts : [member]);
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
      await hold(id, request.method());
      const refusal = refuse(id, request.method());
      if (refusal) return route.fulfill({ status: 502, json: { detail: refusal } });
      if (overrides[id] instanceof Error) return route.fulfill({ status: 502, json: { detail: overrides[id].message } });
      const person = people.find(row => row.user_id === url.searchParams.get("user_id"));
      if (id === "accounts") return route.fulfill({ json: people });
      if ((id === "account" || id === "account_save") && !overrides[id]) {
        if (!person) return route.fulfill({ status: 404, json: { detail: "no household account matches that id" } });
        if (id === "account_save") { const { confirm, ...changes } = request.postDataJSON(); Object.assign(person, changes); }
        return route.fulfill({ json: person });
      }
      if (id === "account_add" && !overrides[id]) {
        const added = { ...member, ...request.postDataJSON(), user_id: "u-created" };
        people.push(added);
        return route.fulfill({ status: 201, json: added });
      }
      const replies = { credentials: [],
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
const pane = page => page.locator(".admin-member");
const asked = page => page.getByRole("alert", { name: "Confirm action" });
// The page notice: under the area control, or in the open member's card after a write on it.
const notice = page => page.locator("main p.notice[role=status]");

test("disable and cleanup require explicit confirmation; cancel sends nothing", async ({ page }) => {
  const { writes, errors } = await mount(page, { failure: true });
  await pane(page).getByRole("button", { name: "Disable", exact: true }).click();
  await expect(asked(page)).toContainText("u-synthetic"); await expect(asked(page)).toContainText("Enabled → Disabled");
  expect(writes).toHaveLength(0);
  await asked(page).getByRole("button", { name: "Cancel", exact: true }).click(); expect(writes).toHaveLength(0);
  await expect(asked(page)).toHaveCount(0);
  await pane(page).getByRole("button", { name: "Disable", exact: true }).click();
  await page.getByRole("button", { name: "Confirm update member", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ id: "account_save", method: "PATCH", target: { user_id: "u-synthetic" }, body: { enabled: false, confirm: true } });
  await expect(notice(page)).toHaveText("Daemon accepted update member.");
  await expect(pane(page).getByRole("button", { name: "Enable", exact: true })).toBeEnabled();
  await expect(page.getByRole("table", { name: "Members" })).toContainText("Disabled");
  await page.getByRole("button", { name: "Maintenance", exact: true }).click(); await expect(page.getByRole("status")).toHaveText("Current daemon state.");
  await select(page, "cleanup"); await form(page).getByRole("button").click();
  await page.getByRole("button", { name: "Confirm clean up" }).click();
  await expect(page.getByRole("status")).toContainText("Daemon unavailable");
  expect(writes.filter(write => write.id === "cleanup")).toHaveLength(1); expect(errors).toEqual([]);
});

test("members show readable values and the list and the first member load without a run step", async ({ page }) => {
  const { reads, errors } = await mount(page, { overrides: { accounts: [member, guest] } });
  const rows = page.getByRole("table", { name: "Members" }).locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("Synthetic member"); await expect(rows.nth(0)).toContainText("member@example.test");
  await expect(rows.nth(0)).toContainText("Enabled"); await expect(rows.nth(0)).toContainText("0 B");
  await expect(rows.nth(0)).toContainText("of 20 GiB"); await expect(rows.nth(0)).toContainText("Not linked");
  await expect(rows.nth(1)).toContainText("Disabled"); await expect(rows.nth(1)).toContainText("312 MiB");
  await expect(rows.nth(1)).toContainText("Linked");
  await expect(page.getByRole("heading", { name: "Members" })).toContainText("2");
  await expect(pane(page)).toContainText("0 B of 20 GiB"); await expect(pane(page)).toContainText("1 running · 2 queued");
  await expect(pane(page)).toContainText("None yet");
  await expect(pane(page)).not.toContainText("1760360280"); await expect(pane(page)).not.toContainText("true");
  expect(reads.map(url => url.pathname)).toEqual(expect.arrayContaining(["/api/manage/accounts", "/api/manage/account"]));
  expect(reads.find(url => url.pathname === "/api/manage/account").searchParams.get("user_id")).toBe("u-synthetic");
  await expect(page.getByLabel("Choose action", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Open Synthetic guest" }).click();
  await expect(page.getByRole("heading", { name: "Synthetic guest", level: 2 })).toBeFocused();
  await expect(pane(page).getByRole("button", { name: "Enable", exact: true })).toBeVisible();
  await expect(page.locator(".admin-footnote")).toHaveText(
    "Not offered here: Owner keys. Hub key cannot mint owner keys On the daemon host: harness keys create.");
  expect(errors).toEqual([]);
});

test("edit sends only the changed fields after a question in the pane", async ({ page }) => {
  const { writes, errors } = await mount(page);
  await pane(page).getByRole("button", { name: "Edit member" }).click();
  await expect(pane(page).getByLabel("Display name")).toHaveValue("Synthetic member");
  await expect(pane(page).getByLabel("Disk quota in bytes")).toHaveValue(String(20 * 1024 ** 3));
  await pane(page).getByRole("button", { name: "Save changes" }).click();
  await expect(notice(page)).toContainText("Nothing to save"); expect(writes).toHaveLength(0);
  await pane(page).getByLabel("Display name").fill("Renamed member");
  await pane(page).getByLabel("Disk quota in bytes").fill(String(10 * 1024 ** 3));
  await expect(pane(page)).toContainText("That is 10 GiB.");
  await pane(page).getByRole("button", { name: "Save changes" }).click();
  await expect(asked(page)).toContainText("Synthetic member → Renamed member");
  await expect(asked(page)).toContainText("20 GiB (21,474,836,480 bytes) → 10 GiB (10,737,418,240 bytes)"); expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Confirm update member", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ id: "account_save", method: "PATCH", target: { user_id: "u-synthetic" },
    body: { display_name: "Renamed member", disk_quota_bytes: 10 * 1024 ** 3, confirm: true } });
  await expect(pane(page).getByRole("button", { name: "Edit member" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Renamed member", level: 2 })).toBeVisible();
  await expect(pane(page)).toContainText("0 B of 10 GiB");
  expect(errors).toEqual([]);
});

test("a quota change the rounded size hides still shows in the question", async ({ page }) => {
  const { errors } = await mount(page, { overrides: { accounts: [{ ...member, disk_quota_bytes: 12 * 1024 ** 3 }] } });
  await pane(page).getByRole("button", { name: "Edit member" }).click();
  await pane(page).getByLabel("Disk quota in bytes").fill(String(12 * 1024 ** 3 - 400 * 1024 ** 2));
  await pane(page).getByRole("button", { name: "Save changes" }).click();
  await expect(asked(page)).toContainText("12 GiB (12,884,901,888 bytes) → 12 GiB (12,465,471,488 bytes)");
  expect(errors).toEqual([]);
});

test("a field left as it was filled is not sent, even when a late read changes the daemon's value", async ({ page }) => {
  let release;
  const detail = new Promise(resolve => { release = resolve; });
  const { writes, errors } = await mount(page, { overrides: { account: { ...member, login: "rebound@example.test" } },
    hold: id => id === "account" ? detail : null });
  await pane(page).getByRole("button", { name: "Edit member" }).click();
  await pane(page).getByLabel("Display name").fill("Renamed member");
  release();
  await expect(pane(page)).toContainText("rebound@example.test");
  await expect(pane(page).getByLabel("Display name")).toHaveValue("Renamed member");
  await pane(page).getByRole("button", { name: "Save changes" }).click();
  await expect(asked(page)).not.toContainText("Exact Tailscale login");
  await page.getByRole("button", { name: "Confirm update member", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body).toEqual({ display_name: "Renamed member", confirm: true });
  expect(errors).toEqual([]);
});

test("a confirmed write takes its question away at once and holds the controls until the reads finish", async ({ page }) => {
  let release;
  const reread = new Promise(resolve => { release = resolve; });
  let wrote = false;
  const { writes, errors } = await mount(page, { hold: (id, method) => {
    if (method !== "GET") wrote = true;
    return wrote && id === "accounts" ? reread : null;
  } });
  await pane(page).getByRole("button", { name: "Disable", exact: true }).click();
  const confirm = page.getByRole("button", { name: "Confirm update member", exact: true });
  await confirm.click();
  await expect(asked(page)).toHaveCount(0);
  await expect.poll(() => writes.length).toBe(1);
  await expect(pane(page).getByRole("button", { name: "Disable", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Open Synthetic member" }).click({ force: true });
  release();
  await expect(pane(page).getByRole("button", { name: "Enable", exact: true })).toBeEnabled();
  expect(writes).toHaveLength(1); expect(errors).toEqual([]);
});

test("Add member is the primary action and sends only what was typed", async ({ page }) => {
  const { writes, errors } = await mount(page);
  await page.getByRole("button", { name: "Maintenance", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Current daemon state.");
  await page.locator("header").getByRole("button", { name: "Add member" }).click();
  await expect(page.getByRole("button", { name: "Accounts", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(pane(page).getByLabel(/Exact Tailscale login/)).toBeFocused();
  await pane(page).getByLabel(/Exact Tailscale login/).fill("new@example.test");
  await pane(page).getByLabel(/Display name/).fill("New member");
  await expect(pane(page).getByLabel(/Exact Tailscale login/)).toHaveValue("new@example.test");
  await pane(page).getByRole("button", { name: "Add member" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ id: "account_add", method: "POST", target: {}, body: { login: "new@example.test", display_name: "New member" } });
  await expect(notice(page)).toHaveText("Daemon accepted add member.");
  await expect(page.getByRole("heading", { name: "New member", level: 2 })).toBeVisible();
  await expect(pane(page)).toHaveAttribute("data-member-id", "u-created");
  await expect(page.getByRole("table", { name: "Members" }).locator("tr.selected")).toContainText("new@example.test");
  expect(errors).toEqual([]);
});

test("erasing the GitHub credential asks inside the danger zone", async ({ page }) => {
  const { writes, errors } = await mount(page);
  const zone = pane(page).locator(".danger-zone");
  await zone.getByRole("button", { name: "Erase GitHub credential" }).click();
  await expect(zone.getByRole("alert", { name: "Confirm action" })).toContainText("Erase credential?");
  await expect(zone.getByRole("button", { name: "Confirm erase GitHub credential" })).toHaveClass(/danger/);
  expect(writes).toHaveLength(0);
  await zone.getByRole("button", { name: "Confirm erase GitHub credential" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ id: "github_reset", method: "POST", target: { user_id: "u-synthetic" }, body: { confirm: true } });
  expect(errors).toEqual([]);
});

test("a link code stays shown when the reread after creating it fails", async ({ page }) => {
  let wrote = false;
  const { errors } = await mount(page, { refuse: (id, method) => {
    if (method !== "GET") wrote = true;
    return wrote && id === "accounts" ? "Daemon did not answer." : null;
  } });
  await pane(page).getByRole("button", { name: "Create link code" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "last list read" })).toContainText("Daemon did not answer.");
  await expect(page.locator("[data-link-code]")).toHaveText("ahg-synthetic-once");
  await expect(page.getByRole("table", { name: "Members" })).toContainText("Synthetic member");
  expect(errors).toEqual([]);
});

test("a failed detail read keeps the list's values and says so in the pane", async ({ page }) => {
  const { errors } = await mount(page, { overrides: { account: new Error("Daemon did not answer.") } });
  await expect(pane(page).getByRole("alert")).toContainText("Daemon did not answer.");
  await expect(pane(page)).toContainText("0 B of 20 GiB");
  expect(errors).toEqual([]);
});

test("Google code is shown once and is cleared by navigation and disposal", async ({ page }) => {
  const { writes, errors } = await mount(page, { overrides: { accounts: [member, guest] } });
  await pane(page).getByRole("button", { name: "Create link code" }).click();
  await expect(page.locator("[data-link-code]")).toHaveText("ahg-synthetic-once");
  await expect(pane(page).getByRole("button", { name: "Dismiss code" })).toBeFocused();
  await page.getByRole("button", { name: "Open Synthetic guest" }).click();
  await expect(page.getByText("ahg-synthetic-once")).toHaveCount(0);
  await page.getByRole("button", { name: "Open Synthetic member" }).click();
  await pane(page).getByRole("button", { name: "Create link code" }).click();
  await expect(page.locator("[data-link-code]")).toBeVisible();
  await page.getByRole("button", { name: "Audit", exact: true }).click();
  await expect(page.getByText("ahg-synthetic-once")).toHaveCount(0);
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
  await pane(page).getByRole("button", { name: "Create link code" }).click();
  await expect(page.locator("[data-link-code]")).toBeVisible();
  await pane(page).getByRole("button", { name: "Cancel link code" }).click();
  await page.getByRole("button", { name: "Confirm cancel Google link code", exact: true }).click();
  await expect(page.getByText("ahg-synthetic-once")).toHaveCount(0);
  await pane(page).getByRole("button", { name: "Create link code" }).click();
  await expect(page.locator("[data-link-code]")).toBeVisible();
  await page.evaluate(() => window.stopAdmin()); await expect(page.getByText("ahg-synthetic-once")).toHaveCount(0);
  expect(writes.map(write => write.id)).toEqual(["google_invite", "google_invite", "google_invite", "google_cancel", "google_invite"]);
  expect(writes[0].target).toEqual({ user_id: "u-synthetic" }); expect(errors).toEqual([]);
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
  await page.setViewportSize({ width, height: 900 }); const { reads, errors } = await mount(page, { overrides: { accounts: [member, guest] } });
  // Rendered targets only: a Host command's lines (and their Copy buttons) stay hidden until its button is pressed.
  const heights = () => page.locator("button, input, select, textarea").evaluateAll(nodes => nodes
    .filter(node => node.getClientRects().length).map(node => node.getBoundingClientRect().height));
  await expect(pane(page)).toContainText("0 B of 20 GiB");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await heights()).every(height => height >= 44)).toBe(true);
  await page.getByRole("button", { name: "Audit", exact: true }).click(); await expect(page.getByRole("button", { name: "Next audit page" })).toBeVisible();
  await expect(form(page).getByRole("button")).toBeEnabled();
  await page.getByRole("button", { name: "Next audit page" }).click();
  await expect.poll(() => reads.some(url => url.searchParams.get("before_id") === "2")).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await heights()).every(height => height >= 44)).toBe(true); expect(errors).toEqual([]);
});


test("a long login at 320px stays inside the members table's box", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  const { errors } = await mount(page, { overrides: { accounts: [{ ...member, login: "householdmemberwithalonglogin@example.test" }] } });
  await expect(pane(page)).toContainText("0 B of 20 GiB");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  const open = await page.getByRole("button", { name: "Open Synthetic member" }).boundingBox();
  expect(open.x + open.width).toBeLessThanOrEqual(320);
  expect(errors).toEqual([]);
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


test("adding projects confirms a fresh before and after list; empty grants no automatic injection", async ({ page }) => {
  const skill = { slug: "safe-skill", title: "Safe skill", projects: ["old-project"], projects_complete: true };
  const { writes, reads } = await mount(page, { overrides: { skills: { installed: [skill], proposals: [] } } });
  await page.getByRole("button", { name: "Skills", exact: true }).click();
  await page.locator(".admin-output").getByRole("button", { name: "Set project allowlist", exact: true }).click();
  const projects = form(page).getByLabel("Allowed projects", { exact: false });
  await expect(projects).toHaveValue("old-project");
  await expect(form(page)).toContainText("empty means no project gets it automatically");
  await projects.fill("old-project\nnew-project"); await form(page).getByRole("button").click();
  const confirmation = asked(page);
  await expect(confirmation).toContainText('"before_projects": [\n    "old-project"');
  await expect(confirmation).toContainText('"after_projects": [\n    "old-project",\n    "new-project"');
  await expect(confirmation).toContainText('"added_projects": [\n    "new-project"');
  expect(reads.filter(url => url.pathname === "/api/manage/skills")).toHaveLength(2);
  expect(writes).toHaveLength(0); await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(writes).toHaveLength(0);
  await projects.fill(""); await form(page).getByRole("button").click();
  await expect(confirmation).toContainText('"after_projects": []');
  await page.getByRole("button", { name: "Confirm set project allowlist" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ id: "skill_projects", method: "PUT", target: { slug: "safe-skill" }, body: { projects: [], confirm: true } });
});

test("a redacted current project list cannot authorize an allowlist change", async ({ page }) => {
  const { writes } = await mount(page, { overrides: { skills: { installed: [
    { slug: "safe-skill", title: "Safe skill", projects: ["[withheld: token-shaped]"], projects_complete: false }], proposals: [] } } });
  await page.getByRole("button", { name: "Skills", exact: true }).click();
  await page.locator(".admin-output").getByRole("button", { name: "Set project allowlist", exact: true }).click();
  await form(page).getByLabel("Allowed projects", { exact: false }).fill("new-project");
  await form(page).getByRole("button").click();
  await expect(page.getByRole("status")).toContainText("current project list is unavailable");
  await expect(asked(page)).toHaveCount(0);
  expect(writes).toHaveLength(0);
});

test("redacted skill and folder rows preserve controls on other rows and audit paging", async ({ page }) => {
  const { writes, reads } = await mount(page, { overrides: {
    skills: { proposals: [], installed: [{ slug: "", title: "[withheld: token-shaped]" }, { slug: "safe-skill", title: "Safe skill" }] },
    remote: { projects: [{ project: "safe-folder", path: "[withheld: token-shaped]", managed: true }] },
    audit: { items: [{ id: 2, target_id: "[withheld: token-shaped]" }], next_before_id: 2 } } });
  await page.getByRole("button", { name: "Skills", exact: true }).click();
  await expect(page.locator(".admin-row").filter({ hasText: "[withheld: token-shaped]" }).getByRole("button")).toHaveCount(0);
  const safe = page.locator(".admin-row").filter({ hasText: "Safe skill" });
  await expect(safe.getByRole("button", { name: "Disable skill", exact: true })).toBeEnabled();
  await safe.getByRole("button", { name: "Disable skill", exact: true }).click();
  await form(page).getByRole("button").click(); await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].target).toEqual({ slug: "safe-skill" });
  await page.getByRole("button", { name: "Runners and projects", exact: true }).click();
  await select(page, "remote"); await form(page).getByRole("button").click();
  await expect(page.locator(".admin-output")).toContainText("[withheld: token-shaped]");
  await expect(page.locator(".admin-output").getByRole("button", { name: "Open folder trust prompt" })).toBeEnabled();
  await expect(page.locator(".admin-output").getByRole("button", { name: "Forget discovered folder" })).toBeEnabled();
  await page.getByRole("button", { name: "Audit", exact: true }).click();
  await expect(page.locator(".admin-output")).toContainText("[withheld: token-shaped]");
  await page.getByRole("button", { name: "Next audit page" }).click();
  await expect.poll(() => reads.some(url => url.searchParams.get("before_id") === "2")).toBe(true);
});

test("only a fully visible scan candidate gets a promotion control", async ({ page }) => {
  await mount(page, { overrides: { scan: { id: "scan-safe", candidates: [
    { id: "bad", suggested_slug: "bad", path: "[withheld: token-shaped]", markers: [".git"], promotion_available: false },
    { id: "good", suggested_slug: "good", path: "/srv/safe", markers: [".git"], promotion_available: true }] } } });
  await page.getByRole("button", { name: "Runners and projects", exact: true }).click();
  await select(page, "scan"); await form(page).getByLabel("Scan id").fill("scan-safe");
  await form(page).getByRole("button").click();
  await expect(page.getByRole("button", { name: "Add folder bad", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add folder good", exact: true })).toBeEnabled();
});

for (const scheme of ["light", "dark"]) {
  test(`the chosen area segment looks selected in the ${scheme} theme`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const { errors } = await mount(page);
    await page.getByRole("button", { name: "Audit", exact: true }).click();
    const area = page.getByRole("group", { name: "Area" });
    await expect(area.locator('[aria-pressed="true"]')).toHaveText("Audit");
    // The chosen segment's outline stands out from the track at least 3:1 (non-text contrast).
    const ratio = await page.evaluate(() => {
      const rgb = colour => colour.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
      const lum = ([r, g, b]) => [r, g, b].map(v => v / 255).map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
      const track = getComputedStyle(document.querySelector(".segmented")).backgroundColor;
      const outline = getComputedStyle(document.querySelector('.segmented [aria-pressed="true"]')).borderTopColor;
      const [a, b] = [outline, track].map(colour => lum(rgb(colour)));
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
    expect(ratio).toBeGreaterThanOrEqual(3);
    expect(errors).toEqual([]);
  });
}
