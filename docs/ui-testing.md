# UI testing contract

Contract version: 1

This contract covers testing the Hub console (`harness_hub/static`) against a disposable Hub and a disposable Agent
Harness Server in **fake model mode** ([agent-harness `docs/ui-testing.md`](https://github.com/dflippojr/agent-harness/blob/main/docs/ui-testing.md)).
The agent-ops `ui-testing-pass` skill reads this file. The commands and the safety boundary are here; the launcher is
[`scripts/ui_instance.py`](../scripts/ui_instance.py) and the committed suite is [`tools/ui-tests`](../tools/ui-tests).

## Scope and isolation

- **Target.** The Hub console, all seven sections: Overview, Settings, Modules, Apps, Pairing requests, Marketplace,
  and Accounts and maintenance. It is served by the Hub (`python -m harness_hub`) from this repository.
- **The daemon.** A real Agent Harness daemon from an agent-harness checkout at the commit CI pins
  (`aa5a9047dbe4bcba6b7ec3348d0cd529c00ebe2b`, the README's minimum daemon) or later, run with `HARNESS_FAKE_MODEL`.
  Every model reply is scripted. The Hub never asks it for one: the Hub reads and changes the daemon's owner API.
- **Loopback only.** The daemon and the Hub both bind `127.0.0.1`. There is no `--public-url`, no `tailscale serve`,
  and no `*.ts.net` address. The launcher refuses ports **8100** (the live daemon's) and **8120** (the live Hub's
  default), and never points anything at them.
- **Disposable data.** Everything lives in the instance directory given to `--dir`:
  - `daemon/config`, `daemon/data` and `daemon/repos`: the daemon's generated config, SQLite databases and workspaces;
  - `hub/`: the Hub's own state directory (Hub key, sign-in codes) and its claim file `hub/claim.json`;
  - `home/`: the `HOME` both processes see, including the daemon CLI's client config;
  - `logs/`: `daemon.log` and `hub.log`, unless `--log-dir` puts them elsewhere;
  - `instance.json`: the handle Teardown uses.

  The real `config/`, data directory, `~/.agent-harness` and `~/.agent-harness-hub` are never read.
- **How isolation is enforced.**
  - The daemon runs fake model mode only because the launcher writes the daemon's `fake-model-instance` marker into
    the instance's own `data_dir`. The daemon refuses `HARNESS_FAKE_MODEL` anywhere else.
  - Both children get an **allowlisted environment** (`PATH`, temp and locale variables, the few that Windows
    needs, and the verify skill's random `VERIFY_RUN_ID`, which its cleanup finds processes by). `HOME`, `USERPROFILE`, `APPDATA` and `LOCALAPPDATA` point inside the instance directory. Owner tokens,
    provider keys, GitHub and Tailscale credentials, and proxies never reach them.
  - Start refuses an occupied port and a non-empty `--dir`, so it never attaches to or reuses another server.
  - `stop` deletes only a directory that holds the launcher's `hub-ui-instance` marker, and kills only the processes
    its handle recorded, checked by start time so a reused pid is left alone.
- **Disabled or stubbed integrations.**
  - Model: scripted. Hosted backends: not configured.
  - Off in the daemon config: notifications (ntfy), backup, homelab, memory library, Claude Remote Control, images
    and image edit, inference endpoint, GPU guard, canary and the Web module.
  - Tailnet identity: none. The Hub trusts `Tailscale-*` headers only from the Windows `tailscaled` service, and the
    launcher never passes `--trust-unverified-identity-headers`, so tailnet sign-in cannot happen. The daemon's
    `allowed_logins` holds one synthetic `owner@example.test`, only because adding household members requires an
    owner allowlist.
  - The one outbound request: on first start the daemon downloads its pinned secret scanner (gitleaks) from GitHub
    releases into `daemon/data/tools`. If that fails, the daemon still starts.
- **Tools.**
  - Python 3.12+ with the Hub and the daemon's requirements:
    `python -m pip install --require-hashes -r requirements-e2e.lock -r requirements-build.lock` and
    `python -m pip install --no-deps --no-build-isolation -e .`
  - An agent-harness checkout: `git clone https://github.com/dflippojr/agent-harness` and check out the pinned commit.
    Name it with `HUB_UI_DAEMON_SRC` (or `--daemon-src`).
  - For the Playwright suite: Node.js 20 or later with npm (CI uses 24).
  - Windows (PowerShell or Git Bash), macOS and Linux all work. No GPU, Docker, Tailscale or llama-server.
- **Secrets.** None are needed: no owner token, model or provider key, GitHub token, Tailscale login or ntfy token.
  The instance creates its own Hub key (by claiming), sign-in codes and synthetic App key. They are valid only for
  that instance and die with it. Keep them out of screenshots, reports and commits.

## Start

Run from the repository root. The command is the same in PowerShell and Git Bash:

```
python scripts/ui_instance.py start --dir <instance-dir> [--daemon-src <checkout>] [--port N] [--daemon-port N]
                                    [--unclaimed] [--no-seed] [--keep] [--log-dir <dir>]
```

For example, with `HUB_UI_DAEMON_SRC` set to the checkout:

- PowerShell: `python scripts/ui_instance.py start --dir "$env:TEMP\hub-ui-pass"`
- Git Bash: `python scripts/ui_instance.py start --dir "$TEMP/hub-ui-pass"`

What it does, in order:

1. Refuses (exit 2, nothing created) a non-empty `--dir`, a busy or live port, or a checkout without fake model mode.
2. Writes the daemon config and marker, starts the daemon (`python -m harness --config-dir ...`) and waits for
   `/health`.
3. Starts the Hub with its own state directory and `--claim-file`, the way the daemon installer does.
4. Unless `--unclaimed`: plays the host. It approves the claim with the daemon's CLI
   (`harness hub approve <id> --match <code>`), signs a seeding session in, and waits until the Hub reports Claimed.
5. Unless `--unclaimed` or `--no-seed`: adds the synthetic seed below.
6. Writes `instance.json` with `"status": "ready"` and prints the summary, ending with one `READY {...}` JSON line
   (`hub`, `daemon`, `dir`, `pid`, `claimed`, `seeded`; no secrets) and `Ctrl+C to stop.`

It then stays in the foreground. Start to READY takes about 10 seconds (CI allows 180).

- **Ports.** `--port` (Hub) and `--daemon-port` default to free ports.
- **Readiness signal.** The `READY` line on stdout, or `instance.json` with `"status": "ready"`. The Hub's own
  `/healthz` answers before seeding ends, so it is not a readiness signal.
- **Logs.** `daemon.log` starts with the `FAKE MODEL MODE` banner. `hub.log` has the Hub's access log and claim
  messages. Keep `--log-dir` outside the instance directory when the logs must survive Teardown.
- **Partial failure.** A failed start prints `FAILED <reason>` on stderr, stops both children, deletes the instance
  directory (unless `--keep`) and exits 1. If the launcher itself is killed, the directory stays: run Teardown.
- **The claim journey.** `--unclaimed` leaves the Hub unclaimed and unseeded. The UI's **Start claim** shows the
  request id and match code. Approve it as the host would with
  `python scripts/ui_instance.py approve --dir <instance-dir> --id <id> --match <code>`.

## Seed and sign-in

**Synthetic seed** (default start). Every start creates the same rows; ids are random, so find rows by name.

| Seed | Where it shows | Content |
| --- | --- | --- |
| This Hub | Overview, Apps, Pairing requests (History, Paired) | Claimed; recorded name "UI test Hub" |
| Synthetic App | Apps | An `app` key, never used ("Never used"); its detail has session caps, retention and Revoke |
| Marketplace entries | Marketplace, Apps (Catalog entries) | The daemon's example entries "Agent Harness Web" and "financial-planner", not paired |
| Synthetic notes | Pairing requests (Needs you) | Scope `sessions` |
| Synthetic approver | Pairing requests (Needs you) | Scopes `sessions` and elevated `approvals` |
| Synthetic member | Accounts and maintenance | Login `member@example.test`, enabled |

The seeded pairing requests come from the launcher, which never keeps their verifiers or match codes. Journeys that
approve create their own request with the daemon's public `POST <daemon>/api/v1/pair/requests` (name, scopes,
`code_challenge`, `code_challenge_method: S256`). Its answer holds the `match_code` the App would show. The daemon
allows **10 requests per source in 10 minutes and 3 pending at once**, and the seed uses two.

**Reset.** There is no in-place reset. Reset is Teardown followed by Start.

**Sign-in.** There is no password. Print a one-time link, as the owner does on the daemon host, and open it:

```
python scripts/ui_instance.py signin-link --dir <instance-dir>
```

The link works once, for ten minutes, and signs **one tab** in: the session is an HttpOnly cookie plus a per-tab
`sessionStorage` value. A new tab or a new browser context needs a new link. A new tab in the same browser context
joins the session the cookie already holds, so earlier tabs stay signed in; a new context starts a session of its own. Opening the link swaps the fragment for
`#/overview`.

**Roles.** The owner is the only role. Members and Apps never use the Hub. Tailnet sign-in needs a real tailnet
device and the Windows `tailscaled` service, so it is not applicable here.

## Journeys

`<hub>` is the READY `hub` URL, signed in. Routes are hash routes. The page title is set before data loads, so the
"ready" column names the first element that needs data.

| ID | Path / ready signal | Seed | Actions | Expected outcome, and after reload | Audit expectation |
| --- | --- | --- | --- | --- | --- |
| HUB-01 navigation | each `#/<section>`; ready: the section's `h1` plus its data (Overview: heading "Daemon"; Settings: spinbutton "Maximum turns"; Modules: "Module switches"; Apps: "Paired apps and keys"; Pairing: "History · last day" (with its count); Marketplace: "Set up financial-planner"; Accounts: "Synthetic member") | default | Visit each section from the nav. Below 900 px, use the **Sections** menu button, then Escape. | The nav link has `aria-current="page"`, and the title is "<Section> · Hub". The menu sets `aria-expanded`. After reload: the same section. On Apps, **Host commands** shows `harness keys list`; **Show host commands** shows every command, hides the buttons and survives a reload. | Not applicable (read-only) |
| HUB-02 sign-in | `<hub>/`; ready: region "Sign in to the Hub" | default | Open signed out. Open `#signin=` with an unknown 43-character code. Open a fresh link, reload, open a second tab, then Sign out. Reuse a used link. | Signed out: only the brand and the sign-in card with its two numbered steps and command, no nav or top bar; **The Hub runs in Docker** opens the container command. Unknown or reused code: an alert, "That sign-in link is not valid. Links work once, for ten minutes. Make a new one below." Link: Overview. After reload: still signed in. A new tab: signed out. Sign out: the sign-in card, also after reload. | Not applicable: the Hub keeps no audit; sign-in is in `hub.log` |
| HUB-03 claim | `--unclaimed` instance, `#/overview`; ready: "This Hub has not claimed the daemon yet." | none | **Start claim**. Read the id and code from `harness hub approve <id> --match <code>`. Run `approve`. | Before: the Claim this daemon card on step 1 and the tiles under "Before you claim". Started: step 2, "Waiting for approval", the code as six boxed digits (an image named "Match code 9 8 7 …"), a countdown that ticks every second and **Stop waiting**. Then within seconds the claim card gives way to the This Hub status tile: "Claimed" and the Hub key. After reload: Claimed. | Accounts and maintenance → Audit: `hub.claim.request`, `hub.claim.approve`, `hub.claim.redeem` |
| HUB-04 settings | `#/settings`; ready: spinbutton "Maximum turns" (the Sessions category opens first) | default | Set Maximum turns to -5 and Validate in the save bar (region "Settings changes"). Reload. Set it to current + 1, Validate, Save, reload. Roll back, then Cancel. Roll back, then **Confirm rollback**, reload. Then edit Maximum turns, open Compaction and come back, and **Discard**; filter "completion tokens", then a filter with no match and **Clear filter**; open **Notifications** from the Controls list. | -5: "must be >= 1" and nothing saved. The edit: a Changed pill and "1 unsaved change · sessions.max_turns N → N+1" in the save bar. Validate: "Validated. … sessions.max_turns: N → N+1" in the save bar. Save: "Saved. Revision r", and the save bar's buttons go. After reload: N+1. Cancel: no change. Confirm: "Rolled back to revision r'" under the page head, and after reload: N. The edit survives the category switch; Discard restores N and hides the bar. The filter leaves "Sessions 1" and Maximum completion tokens; no match shows "No settings match". Notifications shows **Send test notification**. | None in the retained audit (verified): the settings revision is the record |
| HUB-05 modules | `#/modules`; ready: "Module switches" | default | On the **search** card, **Switch search on**, reload, then **Switch search off**. | "Saved search.enabled=true. Revision r. Restart needed …" and "Restart needed to apply saved switches.". After reload: "Switch: on". Off: the reverse. Restart is not exercised: the instance has no supervisor, so Restart reports manual instructions. | None in the retained audit (verified): the settings revision increments |
| HUB-06 apps | `#/apps`; ready: "Paired apps and keys" | default | In the keys table, **Open Synthetic App**: its detail opens beside the list (in its place below 1200 px) with focus on its heading. Set Maximum running, **Save session caps**, reload, reopen. Set Retention days, **Save retention**, Cancel, **Save retention** again and confirm; reload, reopen. | The row reads "Never used" and "App-supplied name". A blank cap shows the daemon default as its placeholder. "Session caps saved.", and after reload the field and "Effective caps: running n". Retention asks "Change retention for Synthetic App?" inside its card; Cancel saves nothing and keeps the typed value; Confirm shows "Retention saved." and the value after reload. Revoke is not exercised: the seeded app is shared, and restoring is host-only. | Audit: `app.limits` and `app.retention` (asserted by the suite) |
| HUB-07 pairing | `#/pairing`; ready: "History · last day" | own request per test | Create a request: its card is under "Needs you", marked "Needs approval". Type a wrong code and Approve; type the App's code and Approve; reload. Create an elevated request: Approve stays disabled until the acknowledgement is checked. Deny, Cancel, Deny, **Confirm deny**, reload. | Wrong code: "the match code does not match the one the App shows" (see #43). Right code: "Approved <name>. The app now fetches its own token.", then a row under "Waiting for the app". Deny asks inside the card ("Deny <name>?"); afterwards the request is a "Denied" row in the History table. | Audit: `pairing_request.create`, `pairing_request.approve`, `pairing_request.deny` |
| HUB-08 marketplace | `#/marketplace`; ready: button "Set up financial-planner" | default | **Set up financial-planner**, Cancel. Set up again, check any acknowledgement, choose an origin if asked, **Approve**, Done, reload, **Withdraw slot for financial-planner**, **Confirm withdraw**, reload. | "Being listed here grants nothing." Consent: "Nothing is armed until you approve." Approve: "Slot armed for financial-planner". After withdraw and reload: "Set up financial-planner" again. | Not checked in pass 1 |
| HUB-09 accounts | `#/admin`; ready: heading "Synthetic member" | default | Choose action "Add member" with a unique login and name, run. Reload. "Update member" with its id and enabled false, run, **Confirm update member**. "Member detail" for it. Open **Audit** and run "Read retained audit". | "Daemon accepted add member." The member is listed after reload. The update asks first and shows the member id; after it, detail shows the member disabled. Audit lists the retained rows. | None in the retained audit (verified): member changes add no row |

**Independence between cases.** HUB-04 and HUB-05 put the setting back. HUB-06 overwrites the seeded app's caps
and retention with fresh values each time. HUB-07 and HUB-09 create uniquely named rows (suffix: project and time).
HUB-08 withdraws what it arms. HUB-03 needs its own `--unclaimed` instance. So one instance serves every device and
theme case when they run one at a time, apart from the pairing request limit above.

## Theme and layout

- **Theme.** The Hub has no theme switch: it follows `prefers-color-scheme`, so Playwright's `colorScheme` and
  `playwright-cli set-color-scheme` select it. Verify with
  `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`: `#f3f4f6` light, `#000000` dark.
- **Breakpoints.** Below 900 px the navigation is a drawer opened by the **Sections** button (`#menu-button`,
  `aria-expanded`) over a dimmed backdrop. From 900 px it is a fixed left rail and there is no top bar. The rail
  groups the sections (Overview; Access: Pairing requests, Apps, Marketplace; Daemon: Settings, Modules, Accounts and
  maintenance), and its footer holds the daemon chip (`role=status`), the **Show host commands** switch
  (`role=switch`, remembered per browser) and **Sign out**; on a phone, open the drawer to reach them. With the
  switch off, each action's CLI equivalent sits behind its own **Host command** button (`aria-expanded`); on, every
  command shows and the buttons go away. The banner slot
  (`#banner-slot`) sits under each page head and shows one daemon message from what the pages already read. Content has a maximum width of
  about 1036 px, so at 2560 px the lines stay readable.
- **Live resize (menu and partly filled form).** On Settings, type a new Maximum turns without saving. Shrink
  1440 → 1280 → 1024 → 768 → 414 → 375 and open the Sections drawer, then grow back to 1440. The typed value
  stays, the rail replaces the drawer with nothing covering the page, the rail links work, there is no horizontal
  overflow and no console error.
- **Confirmations** are in-page `role=alert` sections (Roll back, Restart, retention, revoke, deny, withdraw, account
  updates) with a Confirm and a Cancel button, not dialogs.
- **Touch, drag and PWA.** No gestures: every action is a tap. There is no manifest or service worker, so install and
  offline checks are not applicable.
- **Needs a real device.** Safe-area insets (the page sets `viewport-fit=cover` and pads the drawer with
  `env(safe-area-inset-bottom)`), a real on-screen keyboard, and tailnet sign-in.

## Playwright and CI

- **Repository.** Public, so CI runs on GitHub-hosted runners only (`ubuntu-latest`), never self-hosted.
- **Package.** npm, self-contained in `tools/ui-tests`: `package.json` pins `@playwright/test` exactly, with
  `package-lock.json`. Config `tools/ui-tests/playwright.config.mjs`; tests `tools/ui-tests/tests/*.spec.mjs`.
  `tools/ui-tests/intercepted/*.spec.cjs` are older single-page specs on intercepted synthetic responses over the real
  static files: no instance reads or writes through them. They run as the `intercepted` project (Chromium, light) in
  the same command, and the five instance projects skip that folder. `--project=intercepted` runs only them, though
  `global-setup.mjs` still starts the instance, so `HUB_UI_DAEMON_SRC` must be set.
  `tools/ui-tests/verify/` and `verify.config.mjs` are the verify skill's drive step
  ([.claude/skills/verify](../.claude/skills/verify/SKILL.md)); the suite's config does not run them.
- **Commands.**

  ```
  npm --prefix tools/ui-tests ci
  npm --prefix tools/ui-tests exec -- playwright install chromium webkit
  npm --prefix tools/ui-tests test -- --workers=1
  ```

  Environment: `HUB_UI_DAEMON_SRC` (required), `HUB_UI_PYTHON` (default `python`; Python 3.12+ with the Hub and the
  daemon requirements) and `HUB_UI_INSTANCE_DIR` (default `<tmp>/hub-ui-tests`).
- **Instance and base URL.** `global-setup.mjs` starts one instance in `<HUB_UI_INSTANCE_DIR>/run-<uuid>`, waits for
  READY and passes its URLs to the tests (`HUB_UI_URL`, `HUB_UI_DAEMON_URL`, `HUB_UI_DIR`). There is no fixed port
  and no `reuseExistingServer`. Its teardown runs `ui_instance.py stop`, also after failed tests. The claim spec
  starts and stops its own `--unclaimed` instance.
- **Fixtures.** `signedIn` mints a link with `signin-link` and opens it. `errors` collects page and console errors;
  each test asserts there are none, apart from the Settings page's 404 for the absent memory module (#41) and
  WebKit's report of fetches cancelled by a reload.
- **Projects.** `desktop-light` and `desktop-dark` (Chromium, 1440×900), `phone-light` and `phone-dark` (WebKit with
  the iPhone SE descriptor at 375×667) and `pixel-dark` (Chromium, Pixel 7). The overflow sweep (320, 375, 768 and
  1440 px on every section) and the live-resize test run in the desktop projects. Pairing journeys run in
  `desktop-light`, `desktop-dark` and `phone-dark` because of the daemon's request limit, and the claim in
  `desktop-light`. The `intercepted` project (Chromium, 1440×900, light) runs the specs in `intercepted/` and uses no
  instance.
- **Serial.** `workers: 1`: the journeys share one instance.
- **CI.** The `ui` job in `.github/workflows/ci.yml` checks out the pinned daemon, installs the locked Python and npm
  dependencies and the browsers, runs the suite, then, with `if: always()`, runs `ui_instance.py stop` for every
  `run-*` directory left and uploads the evidence.
- **Reports and traces.** `tools/ui-tests/playwright-report/` and `tools/ui-tests/test-results/` (traces and
  screenshots on failure; both gitignored), and the instance logs in `<HUB_UI_INSTANCE_DIR>/logs/`. CI uploads them as
  `ui-test-evidence` for 7 days. Traces contain that run's session values and used sign-in links; the instance is gone
  by then, so they authorize nothing.

## Teardown

```
python scripts/ui_instance.py stop --dir <instance-dir>
```

It kills the launcher and both children recorded in `instance.json` (only if each pid still has its recorded start
time), then deletes the instance directory. It is safe to repeat, works after a partial start, and refuses a
directory without the launcher's marker. Ctrl+C in the start terminal does the same.

Check that nothing is left: the directory is gone, the READY ports refuse connections
(`curl http://127.0.0.1:<port>/healthz` fails), and no `ui_instance.py`, `harness_hub` or `harness` process with that
`--dir` remains. Logs written with `--log-dir` outside the instance directory, and UI-pass evidence under
`output/ui-pass/` (gitignored), are kept.
