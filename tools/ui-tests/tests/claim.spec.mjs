// HUB-03 first-run claim on its own unclaimed instance: start the claim in the UI, approve it on the "host" with the
// daemon CLI and the code the Hub shows, and watch Overview's This Hub tile turn to Claimed.
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
    const thisHub = page.locator("section").filter({ has: page.getByRole("heading", { name: "This Hub" }) });
    await expect(thisHub).toContainText("This Hub has not claimed the daemon yet.");
    await page.getByRole("button", { name: "Start claim" }).click();
    await expect(thisHub).toContainText("Waiting for approval");
    const text = await thisHub.innerText();
    const [, id, match] = /harness hub approve (\S+) --match (\d{6})/.exec(text);
    await expect(thisHub).toContainText(match);

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
