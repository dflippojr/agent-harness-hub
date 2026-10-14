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
    expect(errors).toEqual([]);
  });
});
