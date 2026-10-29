// HUB-07 and HUB-08 for owner-armed slots: the owner arms a slot for a Marketplace entry, the App claims it with its own
// challenge, the owner types the code the App shows, and the App fetches its token. The App's side is the daemon's
// public API; arming outside the page is the same call Set up and Approve make (that page journey is in
// pairing-market.spec.mjs). Slots are owner-made, so they do not spend the daemon's per-source request limit.
import { appClaims, appRedeems, armSlot, daemonUrl, expect, functionalProject, hubApi, openSection, pkce, test }
  from "./fixtures.mjs";

const ENTRY = "Agent Harness Web";
const card = (page, id) => page.locator(`[data-request-id="${id}"]`);
const group = (page, key) => page.locator(`[data-group="${key}"]`);
const fact = (box, name) => box.locator("dt").filter({ hasText: new RegExp(`^${name}$`) }).locator("+ dd");
const wrongCode = (code) => (code === "000000" ? "111111" : "000000");

test("a claimed slot waits for the typed code, refuses a wrong one, and once confirmed the App redeems its key",
  async ({ signedIn: page, request, errors }, testInfo) => {
    functionalProject(testInfo);
    const { verifier, challenge } = pkce();
    const slot = await armSlot(page, ENTRY);
    await openSection(page, "pairing");
    const box = card(page, slot.id);
    await expect(group(page, "waiting").locator(`[data-request-id="${slot.id}"]`)).toHaveCount(1);
    await expect(box.locator(".pill")).toHaveText("Waiting for the app");
    await expect(box).toContainText("Waiting for the app to claim this slot · sessions");
    await expect(box.getByLabel("Match code the app shows")).toHaveCount(0);

    const { match_code: code } = await appClaims(request, slot.id, challenge);
    await page.reload();
    await expect(group(page, "needs").locator(`[data-request-id="${slot.id}"]`)).toHaveCount(1);
    await expect(box.locator(".decision-head .pill")).toHaveText("Needs confirmation");
    await expect(box).toContainText("Redemption is waiting for confirmation");
    await expect(box).toContainText("If the app shows no code or an error, deny.");
    // The daemon tells only the App its code; the card never carries it before the owner types it.
    expect(await box.evaluate((el, c) => el.outerHTML.includes(c), code)).toBe(false);
    // Owner-armed: the binding is the owner's own, not the requester's word.
    await expect(fact(box, "Bound to")).toContainText("(set by you)");
    await expect(box.locator(".caveat")).toContainText("You armed this slot and set its origin and catalog app id");
    await expect(box).not.toContainText("not verified");
    // The daemon's scope text stays one tap away.
    const text = box.locator('[data-scope="sessions"] .disclosure-text');
    await expect(text).toBeHidden();
    await box.getByText("What each scope allows").click();
    await expect(text).toHaveText(/^This app can start agent tasks on your harness/);

    const input = box.getByLabel("Match code the app shows");
    const confirm = box.getByRole("button", { name: /^Confirm/ });
    await expect(input).toHaveValue("");
    await expect(confirm).toBeDisabled();
    await input.fill("   ");
    await expect(confirm).toBeDisabled();
    await input.fill(wrongCode(code));
    await confirm.click();
    await expect(box.locator("p.notice.bad")).toHaveText("The match code does not match the one the App shows.");
    await expect(box).toContainText("Redemption is waiting for confirmation");
    await input.fill(` ${code} `);
    await confirm.click();
    await expect(group(page, "waiting").locator(`[data-request-id="${slot.id}"][data-state="approved"]`)).toBeVisible();
    await expect(box).toContainText("Waiting for the app to fetch its own token");
    await expect(box.getByLabel("Match code the app shows")).toHaveCount(0);

    // The App redeems; the request is a History row naming the key, and Troubleshooting says how to undo either kind.
    const key = await appRedeems(request, slot.id, verifier);
    await page.reload();
    const history = page.locator(`[data-group="history"] tr[data-request-id="${slot.id}"]`);
    await expect(history.locator("td")).toHaveText([ENTRY, "App", "Paired", "sessions", /\d/, key.id]);
    const recovery = page.locator("details.pairing-recovery");
    await expect(recovery.getByText("harness pairing-requests list", { exact: true })).toBeHidden();
    await recovery.getByText("Troubleshooting: the app says the slot was already claimed").click();
    await expect(recovery).toContainText("For a redeemed row, revoke its key_id. For an approved or claimed row, deny it.");
    for (const cli of ["harness pairing-requests list", "harness keys revoke <key_id>", "harness pairing-requests deny <id>"]) {
      await expect(recovery.getByText(cli, { exact: true })).toBeVisible();
    }
    // Revoked again, so the entry reads Not paired for the other journeys.
    expect((await hubApi(page, `apps/${key.id}/revoke`, { method: "DELETE", body: { confirm: true } })).status).toBeLessThan(300);
    expect(errors.filter((e) => !/status of 400/.test(e))).toEqual([]);
  });

test("Withdraw asks inside the waiting row; Cancel keeps the slot and Confirm moves it to History",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    const slot = await armSlot(page, ENTRY);
    await openSection(page, "pairing");
    const box = card(page, slot.id);
    const withdraw = box.getByRole("button", { name: `Withdraw ${ENTRY}` });
    await withdraw.click();
    const confirm = box.getByRole("alert");
    await expect(confirm).toContainText(`Withdraw slot ${slot.id} from a native app. The app cannot use it afterwards; it must ask again.`);
    await expect(box.getByRole("button", { name: /^Withdraw/ })).toHaveCount(0);
    await confirm.getByRole("button", { name: "Cancel" }).click();
    await expect(withdraw).toBeFocused();
    await withdraw.click();
    await box.getByRole("button", { name: "Confirm withdraw" }).click();
    await expect(page.locator(`[data-group="history"] [data-request-id="${slot.id}"][data-state="denied"]`)).toBeVisible();
    await expect(page.getByText(`Withdrew ${ENTRY}.`)).toBeVisible();
    expect(errors).toEqual([]);
  });

test("on Marketplace a slot counts down, a claim needs the typed code, and Withdraw asks inside its own slot",
  async ({ signedIn: page, request, errors }, testInfo) => {
    functionalProject(testInfo);
    const { challenge } = pkce();
    const claimed = await armSlot(page, ENTRY);
    const armed = await armSlot(page, ENTRY);
    const { match_code: code } = await appClaims(request, claimed.id, challenge);
    await openSection(page, "marketplace");
    const entry = page.getByRole("region", { name: ENTRY, exact: true });
    const box = entry.locator(`[data-slot="${claimed.id}"]`);
    expect(await box.evaluate((el, c) => el.outerHTML.includes(c), code)).toBe(false);
    await box.getByRole("button", { name: /^Host commands?$/ }).click();
    await expect(box.getByText(`harness pairing-requests confirm ${claimed.id} --match <code>`, { exact: true })).toBeVisible();
    await expect(box).toContainText("Redemption is waiting for confirmation.");
    const input = box.getByLabel("Match code the app shows");
    const confirm = box.getByRole("button", { name: `Confirm ${ENTRY}` });
    await expect(input).toHaveValue("");
    await expect(confirm).toBeDisabled();
    await input.fill(wrongCode(code));
    await confirm.click();
    await expect(entry).toContainText("the match code does not match the one the App shows", { ignoreCase: true });
    await entry.locator(`[data-slot="${claimed.id}"]`).getByLabel("Match code the app shows").fill(` ${code} `);
    await entry.locator(`[data-slot="${claimed.id}"]`).getByRole("button", { name: `Confirm ${ENTRY}` }).click();
    await expect(page.getByText(new RegExp(`Confirmed ${ENTRY}`))).toBeVisible();

    // The armed slot: its countdown ticks between polls, and it offers no code field until an App claims it.
    const other = entry.locator(`[data-slot="${armed.id}"]`);
    await expect(other.getByText("Waiting for the app", { exact: true })).toBeVisible();
    await expect(other.locator(".countdown")).toHaveText(/^\d+:\d\d left$/);
    const before = await other.locator(".countdown").textContent();
    await expect(other.locator(".countdown")).not.toHaveText(before, { timeout: 5_000 });
    await expect(other.getByLabel("Match code the app shows")).toHaveCount(0);
    const withdraw = other.getByRole("button", { name: `Withdraw slot for ${ENTRY}` });
    await withdraw.click();
    const ask = other.getByRole("alert");
    await expect(ask.getByRole("heading", { name: `Withdraw the slot for ${ENTRY}?` })).toBeVisible();
    await expect(ask.getByRole("button", { name: "Confirm withdraw" })).toBeFocused();
    await ask.getByRole("button", { name: "Cancel" }).click();
    await expect(other.getByRole("alert")).toHaveCount(0);
    await expect(withdraw).toBeFocused();
    await withdraw.click();
    await other.getByRole("button", { name: "Confirm withdraw" }).click();
    await expect(entry.locator(`[data-slot="${armed.id}"]`)).toHaveCount(0);
    // The confirmed slot is the App's to redeem; deny it so the entry is left with no slots.
    expect((await hubApi(page, `pairing/${claimed.id}/deny`, { method: "POST", body: { confirm: true } })).status).toBe(200);
    expect(errors.filter((e) => !/status of 400/.test(e))).toEqual([]);
  });

test("on Marketplace a refused Confirm focuses its notice in view, and a confirm or withdraw that succeeds does not",
  async ({ signedIn: page, request, errors }, testInfo) => {
    functionalProject(testInfo);
    const { challenge } = pkce();
    const claimed = await armSlot(page, ENTRY);
    const armed = await armSlot(page, ENTRY);
    const { match_code: code } = await appClaims(request, claimed.id, challenge);
    await openSection(page, "marketplace");
    const entry = page.getByRole("region", { name: ENTRY, exact: true });
    const notice = page.locator('[data-field="notice"]');
    const slot = () => entry.locator(`[data-slot="${claimed.id}"]`);
    await slot().getByLabel("Match code the app shows").fill(wrongCode(code));
    await slot().getByRole("button", { name: `Confirm ${ENTRY}` }).click();
    await expect(notice).toHaveClass(/\bbad\b/);
    await expect(notice).toContainText("the match code does not match the one the App shows", { ignoreCase: true });
    await expect(notice).toBeFocused();
    await expect(notice).toBeInViewport();

    await slot().getByLabel("Match code the app shows").fill(code);
    await slot().getByRole("button", { name: `Confirm ${ENTRY}` }).click();
    await expect(notice).toHaveText(new RegExp(`Confirmed ${ENTRY}`));
    await expect(notice).not.toBeFocused();

    const other = entry.locator(`[data-slot="${armed.id}"]`);
    await other.getByRole("button", { name: `Withdraw slot for ${ENTRY}` }).click();
    await other.getByRole("button", { name: "Confirm withdraw" }).click();
    await expect(entry.locator(`[data-slot="${armed.id}"]`)).toHaveCount(0);
    await expect(notice).not.toHaveClass(/\bbad\b/);
    await expect(notice).not.toBeFocused();
    // The confirmed slot is the App's to redeem; deny it so the entry is left with no slots.
    expect((await hubApi(page, `pairing/${claimed.id}/deny`, { method: "POST", body: { confirm: true } })).status).toBe(200);
    expect(errors.filter((e) => !/status of 400/.test(e))).toEqual([]);
  });

test("Marketplace entries say they are local and unverified, and nothing is fetched from an entry",
  async ({ signedIn: page, errors }, testInfo) => {
    functionalProject(testInfo);
    const outside = [];
    page.on("request", (r) => {
      const { hostname } = new URL(r.url());
      if (!["127.0.0.1", "localhost"].includes(hostname) && !r.url().startsWith("data:")) outside.push(r.url());
    });
    await openSection(page, "marketplace");
    await expect(page.locator(".banner.info").filter({ hasText: "Being listed here grants nothing." })).toBeVisible();
    await expect(page.getByRole("heading", { name: /^Entries \d+$/ })).toBeVisible();
    for (const name of [ENTRY, "financial-planner"]) {
      const entry = page.getByRole("region", { name, exact: true });
      await expect(entry.getByText("Local · not verified", { exact: true })).toBeVisible();
      await expect(entry.locator('[data-field="state"]')).toHaveText("Not paired");
      // No icon in the daemon's example entries: a letter stands in, and no image is requested.
      await expect(entry.locator(".entry-icon")).toHaveText(name[0]);
    }
    await expect(page.locator("main img")).toHaveCount(0);
    expect(await page.locator("main > *").last().evaluate((el) => el.matches("details.pairing-recovery"))).toBe(true);
    expect(outside).toEqual([]);
    expect(daemonUrl()).toMatch(/^http:\/\/127\.0\.0\.1:/);
    expect(errors).toEqual([]);
  });
