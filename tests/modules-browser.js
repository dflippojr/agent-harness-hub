// playwright-cli run-code --filename=tests/modules-browser.js
// Start on a signed-in Modules page backed by tests.conftest.FakeDaemon.
async page => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const main = page.locator("#main");
  await page.getByRole("heading", { name: "Modules", exact: true }).waitFor();
  await page.getByRole("button", { name: "Switch notifications on", exact: true }).waitFor();
  if (await main.getByText("Module status failed.", { exact: true }).count() !== 1) throw Error("missing status error");
  if (await main.getByRole("button", { name: /Switch missing/ }).count()) throw Error("absent module is switchable");
  const writes = [];
  page.on("request", request => {
    if (request.method() === "PATCH") writes.push(request.postDataJSON());
  });
  // Stale revisions are explicit failures; the UI reloads, without retrying a write.
  await page.route("**/api/settings/config", async route => {
    if (route.request().method() !== "PATCH") return route.continue();
    await route.fulfill({ status: 409, json: { detail: "Synthetic stale revision",
      error: { code: "revision_conflict" } } });
  });
  await page.getByRole("button", { name: "Switch notifications on", exact: true }).click();
  await main.getByText(/Reloaded the current revision/).waitFor();
  if (writes.length !== 1 || writes[0].revision !== 3 || writes[0].changes["notify.enabled"] !== true) {
    throw Error("switch must use the schema key and revision, with one write");
  }
  await page.unroute("**/api/settings/config");
  await page.getByRole("button", { name: "Switch search on", exact: true }).click();
  await page.getByRole("link", { name: "Restart in Settings", exact: true }).waitFor();
  await main.getByText("Switch: on (saved; restart needed)", { exact: true }).waitFor();
  if (writes.length !== 2 || writes[1].changes["search.enabled"] !== true) throw Error("restart switch missing");
  await page.getByRole("link", { name: "Restart in Settings", exact: true }).click();
  await page.getByRole("button", { name: "Restart", exact: true }).click();
  await page.getByRole("heading", { name: "Restart the daemon?", exact: true }).waitFor();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Restart", exact: true }).click();
  await page.getByRole("button", { name: "Confirm restart", exact: true }).click();
  await main.getByText("Daemon is back. Settings reloaded.", { exact: true }).waitFor();
  await page.evaluate(() => { location.hash = "#/modules"; });
  await page.getByRole("button", { name: "Switch search off", exact: true }).waitFor();
  const good = await page.evaluate(async () => {
    const { api } = await import("/static/lib/api.mjs");
    return api("modules");
  });
  // Named errors retain the full inventory and scalar markup remains literal text.
  good.modules[0].status = { message: "<img src=x onerror=alert(1)>" };
  await page.route("**/api/modules", route => route.fulfill({ json: {
    ...good, state: "error", error: "hub.entries: invalid JSON", stale: true } }));
  await page.getByRole("button", { name: "Refresh modules", exact: true }).click();
  await main.getByText("hub.entries: invalid JSON", { exact: true }).waitFor();
  await main.getByText("<img src=x onerror=alert(1)>", { exact: true }).waitFor();
  if (await main.locator("img").count()) throw Error("status markup was parsed");
  await main.getByText(/Last good inventory from/).waitFor();
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    if (width < 900) await page.waitForFunction(() => document.getElementById("nav").getBoundingClientRect().right <= 0);
    const layout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
      small: [...document.querySelectorAll("button,.nav-link,.restart-link")].filter(el => {
        const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.height < 44;
      }).map(el => el.textContent) }));
    if (layout.scroll > layout.width || layout.small.length) throw Error(JSON.stringify(layout));
    await page.screenshot({ path: ".validation/modules-" + width + ".png", fullPage: true });
  }
  await page.unroute("**/api/modules");
  for (const state of ["disabled", "absent"]) {
    await page.route("**/api/settings/config", async route => {
      const response = await route.fetch();
      const config = await response.json();
      if (state === "disabled") config.settings.find(row => row.key === "hub.enabled").effective = false;
      await route.fulfill({ response, json: config });
    });
    await page.route("**/api/modules", route => route.fulfill({ json: { state,
      error: state === "disabled" ? "Hub inventory is disabled." : "The Hub inventory module is not installed.",
      modules: null, fetched_at: null, stale: false } }));
    await page.getByRole("button", { name: "Refresh modules", exact: true }).click();
    await main.getByText(state === "disabled" ? "Hub inventory is disabled." :
      "The Hub inventory module is not installed.", { exact: true }).waitFor();
    const switches = await main.getByRole("button", { name: /Switch hub on/ }).count();
    if (switches !== (state === "disabled" ? 1 : 0)) throw Error("inventory recovery switch is wrong");
    await page.unroute("**/api/modules");
    await page.unroute("**/api/settings/config");
  }
  if (errors.length) throw Error(errors.join("; "));
  console.log("PASS: schema key/revision, conflict without retry, pending switch, confirmed Settings restart, last-good error, text safety, disabled/absent recovery, desktop/phone and 44px targets");
}
