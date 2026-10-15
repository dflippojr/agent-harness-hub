// HUB-02 sign-in: only a one-time link from the host opens the Hub; the session lasts across reloads in that tab and
// ends on sign-out. No tailnet identity exists on this loopback instance.
import { signinLink } from "../instance.mjs";
import { expect, hubUrl, signIn, test } from "./fixtures.mjs";

const signinCard = (page) => page.getByRole("region", { name: "Sign in to the Hub" });

test("signed out, an unknown link is refused and a fresh link signs in", async ({ page, errors }) => {
  await page.goto(`${hubUrl()}/`);
  await expect(signinCard(page)).toBeVisible();
  await expect(signinCard(page)).toContainText("python -m harness_hub signin-link");
  await expect(signinCard(page).getByRole("listitem")).toHaveCount(2);
  // No top bar while signed out: the card carries the brand, at every width.
  await expect(page.locator(".topbar")).toBeHidden();
  const docker = signinCard(page).getByText("The Hub runs in Docker");
  await docker.click();
  await expect(signinCard(page)).toContainText("docker exec <hub container> python -m harness_hub signin-link");
  await expect(page.getByRole("button", { name: /^Continue as/ })).toHaveCount(0);
  await expect(page.locator("#nav a[data-section]")).toHaveCount(0);
  await expect(page.locator("#nav")).toBeHidden();

  await page.goto(`${hubUrl()}/#signin=${"x".repeat(43)}`);
  await page.reload();
  await expect(signinCard(page).getByRole("alert")).toHaveText(
    "That sign-in link is not valid.Links work once, for ten minutes. Make a new one below.");
  await expect(page).toHaveURL(/#\/overview$/);

  await signIn(page);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  // The per-tab session value lives in sessionStorage: a new tab starts signed out.
  const other = await page.context().newPage();
  await other.goto(`${hubUrl()}/#/apps`);
  await expect(signinCard(other)).toBeVisible();
  await other.close();

  // Sign out sits in the rail's footer; below 900 px it is in the bottom bar's More sheet.
  const more = page.getByRole("button", { name: "More" });
  if (await more.isVisible()) await more.click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(signinCard(page)).toBeVisible();
  await page.reload();
  await expect(signinCard(page)).toBeVisible();
  // The browser reports the refused sign-in (401) as a failed resource load.
  expect(errors.filter((e) => !/status of 401/.test(e))).toEqual([]);
});

test("a used link does not sign in twice", async ({ page }) => {
  const once = signinLink(process.env.HUB_UI_DIR);
  await page.goto(once);
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
  const second = await page.context().newPage();
  await second.goto(once);
  await expect(signinCard(second).getByRole("alert")).toHaveText(/sign-in link is not valid/);
});
