// HUB-03 first-run claim on its own unclaimed instance: start the claim in the UI, approve it on the "host" with the
// daemon CLI and the code the Hub shows, and watch the claim card give way to Overview's This Hub tile: Claimed.
import { approveClaim, startInstance } from "../instance.mjs";
import { expect, signIn, test } from "./fixtures.mjs";

let instance;

test.describe("claim", () => {
  test.beforeAll(async ({}, testInfo) => {
    // One claim on a dedicated instance is enough: it takes a second daemon.
    test.skip(testInfo.project.name !== "desktop-light", "claim runs in desktop-light only");
    test.setTimeout(240_000);
    instance = await startInstance({ unclaimed: true });
  });
  test.afterAll(async () => {
    await instance?.stop();
    instance = undefined;
  });

  test("start, approve on the host, then claimed after reload", async ({ page, errors }) => {
    await signIn(page, instance.dir);
    // Before the claim, a page that needs the daemon says so once and links to Overview.
    await page.goto(`${instance.hub}/#/pairing`);
    const unclaimed = page.locator(".card.empty");
    await expect(unclaimed.getByRole("heading", { name: "Claim this Hub first" })).toBeVisible();
    await expect(unclaimed.getByRole("link", { name: "Go to Overview" })).toHaveAttribute("href", "#/overview");
    await expect(page.getByRole("alert")).toHaveCount(0);
    await unclaimed.getByRole("link", { name: "Go to Overview" }).click();
    const card = page.getByRole("region", { name: "Claim this daemon" });
    const steps = card.getByRole("list", { name: "Claim steps" });
    await expect(card).toContainText("This Hub has not claimed the daemon yet.");
    await expect(steps.locator("[aria-current=step]")).toHaveText("1Start the claim");
    await expect(page.getByRole("heading", { name: "Before you claim" })).toBeVisible();
    await page.getByRole("button", { name: "Start claim" }).click();
    await expect(card).toContainText("Waiting for approval");
    await expect(steps.locator("[aria-current=step]")).toHaveText("2Check the code on the daemon host");
    const text = await card.innerText();
    const [, id, match] = /harness hub approve (\S+) --match (\d{6})/.exec(text);
    // This host-only step stays visible with Show host commands off.
    await expect(page.getByRole("switch", { name: "Show host commands" })).not.toBeChecked();
    await expect(card.getByText(`harness hub approve ${id} --match ${match}`, { exact: true })).toBeVisible();
    // The match code is six boxed digits, read out one by one, beside a live countdown and a quiet Stop waiting.
    await expect(card.getByRole("img", { name: `Match code ${match.split("").join(" ")}` })).toBeVisible();
    const left = card.locator(".claim-clock");
    await expect(left).toHaveText(/^\d+:\d\d left$/);
    const before = await left.innerText();
    await expect(left).not.toHaveText(before, { timeout: 5_000 });
    await expect(card.getByRole("button", { name: "Stop waiting" })).toBeVisible();

    approveClaim(instance.dir, id, match);
    // Once claimed, the claim card gives way to the This Hub status tile.
    const tile = page.getByRole("group", { name: "Status" }).locator("section").filter({ has: page.getByRole("heading", { name: "This Hub" }) });
    await expect(tile).toContainText("Claimed", { timeout: 60_000 });
    await expect(tile).toContainText(/Key \S+ · since /);
    await expect(page.getByRole("button", { name: "Start claim" })).toHaveCount(0);
    await page.reload();
    await expect(tile).toContainText("Claimed");
    // Unseeded, the daemon holds only this Hub's own key: Apps says none are paired and where they come from.
    await page.goto(`${instance.hub}/#/apps`);
    const empty = page.locator(".card.empty");
    await expect(empty.getByRole("heading", { name: "No apps paired yet" })).toBeVisible();
    await expect(empty).toContainText("Only this Hub's own key is listed.");
    await expect(empty.getByRole("link", { name: "Open Marketplace" })).toHaveAttribute("href", "#/marketplace");
    // The unclaimed pages' 409s are failed resource loads in the console.
    expect(errors.filter((e) => !/status of 409/.test(e))).toEqual([]);
  });
});
