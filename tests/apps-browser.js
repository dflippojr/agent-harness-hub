// playwright-cli run-code --filename=tests/apps-browser.js
// Start signed in to a Hub backed by tests.conftest.FakeDaemon. One browser worker.
async page => {
  const errors = [], writes = [], bodies = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    if (request.url().includes("/api/apps") && request.method() !== "GET") writes.push(request);
  });
  page.on("response", async response => {
    if (response.url().includes("/api/apps")) bodies.push(await response.text());
  });
  await page.getByRole("link", { name: "Apps", exact: true }).click();
  await page.getByRole("heading", { name: "Apps", exact: true }).waitFor();
  const main = page.locator("#main");
  const app = id => main.locator('[data-app-id="' + id + '"]');
  const detail = id => main.locator('[data-detail-id="' + id + '"]');
  const open = async (id, name) => {
    if (!await detail(id).count()) await app(id).getByRole("button", { name }).click();
    await detail(id).waitFor();
    return detail(id);
  };
  await app("k-minute").getByText("Active", { exact: true }).waitFor();
  for (const [id, state] of [["never", "Never used"], ["twenty", "Idle"], ["three", "Idle"],
    ["thirty", "Stale"], ["revoked", "Revoked · erased"], ["pending", /^Revoked · erases /]]) {
    await app("k-" + id).locator(".app-status").getByText(state).waitFor();
  }
  await app("k-old").getByText("Token 90+ days", { exact: true }).waitFor();
  await app("k-errors").getByText("Errors, 24 h", { exact: true }).waitFor();
  await (await open("k-old", "Open Synthetic old")).getByText("Token is at least 90 days old. No automatic expiry.", { exact: true }).waitFor();
  await (await open("k-errors", "Open Synthetic errors")).getByText(/^Errors in the last 24 hours/).waitFor();
  const hub = main.locator("tr[data-app-id]").filter({ hasText: "This Hub" });
  await hub.getByRole("button", { name: "Open This Hub" }).click();
  await main.locator(".apps-detail").getByText("harness hub release --confirm", { exact: true }).waitFor();
  if (await main.locator(".apps-detail").getByRole("button", { name: /Revoke/ }).count()) throw Error("Hub revoke offered");
  if (await main.getByRole("button", { name: /^Restore$|^Confirm restore/ }).count()) throw Error("restore offered");
  await (await open("k-pending", "Restore command for Synthetic pending")).getByText("harness apps restore k-pending", { exact: true }).waitFor();
  const minute = await open("k-minute", "Open Synthetic minute");
  await minute.getByRole("button", { name: "Revoke Synthetic minute", exact: true }).click();
  await minute.getByRole("heading", { name: "Revoke Synthetic minute?", exact: true }).waitFor();
  if (writes.length) throw Error("revoke before confirmation");
  await minute.getByRole("button", { name: "Cancel", exact: true }).click();
  if (writes.length) throw Error("cancel wrote");
  await minute.getByRole("button", { name: "Revoke Synthetic minute", exact: true }).click();
  await minute.getByRole("button", { name: "Confirm revoke Synthetic minute", exact: true }).click();
  await app("k-minute").getByText(/^Revoked · erases /).waitFor();
  if (writes.length !== 1 || writes[0].method() !== "DELETE" || writes[0].postDataJSON().confirm !== true) {
    throw Error("revoke must be one confirmed DELETE");
  }
  const twenty = await open("k-twenty", "Open Synthetic twenty");
  await twenty.getByLabel("Maximum running (1–100)").fill("4");
  await twenty.getByLabel("Maximum queued (1–1000)").fill("");
  await twenty.getByRole("button", { name: "Save session caps", exact: true }).click();
  await main.getByText("Session caps saved.", { exact: true }).waitFor();
  if (writes.length !== 2 || writes[1].postDataJSON().max_running !== 4 || writes[1].postDataJSON().max_queued !== null) {
    throw Error("caps payload mismatch");
  }
  await twenty.getByLabel("Retention days").fill("7.5");
  await twenty.getByRole("button", { name: "Save retention", exact: true }).click();
  await twenty.getByRole("heading", { name: "Change retention for Synthetic twenty?", exact: true }).waitFor();
  if (writes.length !== 2) throw Error("retention before confirmation");
  await twenty.getByRole("button", { name: "Confirm change retention for Synthetic twenty", exact: true }).click();
  await main.getByText("Retention saved.", { exact: true }).waitFor();
  if (writes.length !== 3 || writes[2].postDataJSON().retention_days !== 7.5) throw Error("retention missing");
  const good = await page.evaluate(async () => (await import("/static/lib/api.mjs")).api("apps"));
  good.apps.find(row => row.id === "k-never").name = "<img src=x onerror=alert(1)>";
  await page.route("**/api/apps", route => route.fulfill({ json: {
    ...good, state: "error", error: "hub.entries: invalid JSON", stale: true } }));
  await page.getByRole("button", { name: "Refresh apps", exact: true }).click();
  await main.getByText("hub.entries: invalid JSON", { exact: true }).waitFor();
  await main.getByText(/Last good inventory from/).waitFor();
  await app("k-never").getByText("<img src=x onerror=alert(1)>", { exact: true }).waitFor();
  if (await main.locator("img").count()) throw Error("markup parsed");
  const enabled = await main.getByRole("button", { name: /^(Revoke|Save) / }).evaluateAll(nodes => nodes.filter(n => !n.disabled).length);
  if (enabled) throw Error("stale inventory allowed management");
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    if (width < 900) await page.waitForFunction(() => document.getElementById("nav").getBoundingClientRect().right <= 0);
    const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
      small: [...document.querySelectorAll("button,input,.nav-link")].filter(el => {
        const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.height < 44 || r.width < 44);
      }).map(el => el.textContent) }));
    if (layout.scroll > layout.width || layout.small.length) throw Error(JSON.stringify(layout));
    await page.screenshot({ path: ".validation/apps-" + width + ".png", fullPage: true });
  }
  await page.unroute("**/api/apps");
  if (bodies.some(body => /(?:ha|ho|hp)-[A-Za-z0-9_-]{4,}|"prefix"/.test(body))) throw Error("secret response");
  if (writes.some(request => request.url().includes("restore"))) throw Error("restore was called");
  if (errors.length) throw Error(errors.join("; "));
  console.log("PASS: readable states, row warnings, 90-day warning, protected Hub, in-card confirmed revoke, host CLI restore, caps, in-card retention, stale/error state, text safety, desktop/1024/phone, 44px targets and secret-free responses");
}
