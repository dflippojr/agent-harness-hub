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
  await app("k-minute").getByText("active", { exact: true }).waitFor();
  for (const [id, state] of [["never", "never_used"], ["twenty", "idle"], ["three", "idle"],
    ["thirty", "stale"], ["revoked", "revoked"], ["pending", "erasure_pending"]]) {
    await app("k-" + id).getByText(state, { exact: true }).waitFor();
  }
  await app("k-old").getByText("Token is at least 90 days old. No automatic expiry.", { exact: true }).waitFor();
  await app("k-errors").getByText("Errors in the last 24 hours", { exact: true }).waitFor();
  if (await app("k-sample").getByRole("button", { name: /Revoke/ }).count()) throw Error("Hub revoke offered");
  if (await main.getByRole("button", { name: /^Restore|^Confirm restore/ }).count()) throw Error("restore offered");
  await app("k-pending").getByText("harness apps restore k-pending", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Revoke Synthetic minute", exact: true }).click();
  await page.getByRole("heading", { name: "revoke Synthetic minute?", exact: true }).waitFor();
  if (writes.length) throw Error("revoke before confirmation");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  if (writes.length) throw Error("cancel wrote");
  await page.getByRole("button", { name: "Revoke Synthetic minute", exact: true }).click();
  await page.getByRole("button", { name: "Confirm revoke Synthetic minute", exact: true }).click();
  await app("k-minute").getByText("erasure_pending", { exact: true }).waitFor();
  if (writes.length !== 1 || writes[0].method() !== "DELETE" || writes[0].postDataJSON().confirm !== true) {
    throw Error("revoke must be one confirmed DELETE");
  }
  await page.getByRole("button", { name: "Manage Synthetic twenty", exact: true }).click();
  await page.getByRole("heading", { name: "Manage Synthetic twenty", exact: true }).waitFor();
  await page.getByLabel("Maximum running (1–100; blank uses default)").fill("4");
  await page.getByLabel("Maximum queued (1–1000; blank uses default)").fill("");
  await page.getByRole("button", { name: "Save session caps", exact: true }).click();
  await main.getByText("Session caps saved.", { exact: true }).waitFor();
  if (writes.length !== 2 || writes[1].postDataJSON().max_running !== 4 || writes[1].postDataJSON().max_queued !== null) {
    throw Error("caps payload mismatch");
  }
  await page.getByRole("button", { name: "Manage Synthetic twenty", exact: true }).click();
  await page.getByRole("heading", { name: "Manage Synthetic twenty", exact: true }).waitFor();
  await page.getByLabel("Retention days (blank keeps until deleted)").fill("7.5");
  await page.getByRole("button", { name: "Save retention", exact: true }).click();
  await page.getByRole("heading", { name: "change retention for Synthetic twenty?", exact: true }).waitFor();
  if (writes.length !== 2) throw Error("retention before confirmation");
  await page.getByRole("button", { name: "Confirm change retention for Synthetic twenty", exact: true }).click();
  await main.getByText("Retention saved.", { exact: true }).waitFor();
  if (writes.length !== 3 || writes[2].postDataJSON().retention_days !== 7.5) throw Error("retention missing");
  const good = await page.evaluate(async () => (await import("/static/lib/api.mjs")).api("apps"));
  good.apps.find(row => row.id === "k-never").name = "<img src=x onerror=alert(1)>";
  await page.route("**/api/apps", route => route.fulfill({ json: {
    ...good, state: "error", error: "hub.entries: invalid JSON", stale: true } }));
  await page.getByRole("button", { name: "Refresh apps", exact: true }).click();
  await main.getByText("hub.entries: invalid JSON", { exact: true }).waitFor();
  await main.getByText(/Last good inventory from/).waitFor();
  await main.getByRole("heading", { name: "<img src=x onerror=alert(1)> · k-never", exact: true }).waitFor();
  if (await main.locator("img").count()) throw Error("markup parsed");
  const enabled = await main.getByRole("button", { name: /^(Revoke|Manage) / }).evaluateAll(nodes => nodes.filter(n => !n.disabled).length);
  if (enabled) throw Error("stale inventory allowed management");
  for (const width of [1280, 390]) {
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
  console.log("PASS: inventory states, 90-day warning, metrics, protected Hub, confirmed revoke, host CLI restore, caps, retention, stale/error state, text safety, desktop/phone, 44px targets and secret-free responses");
}
