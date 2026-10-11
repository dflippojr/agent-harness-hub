# agent-harness-hub

**Hub** is the standalone admin console for one [Agent Harness](https://github.com/dflippojr/agent-harness) daemon:
think of a home router's admin page plus marketplace access. It is its own app with its own small Python server,
running on the daemon host on its own port. It administers the daemon only through the owner API
(`/api/admin/v1`) with the daemon's exclusive `hub` key, which it obtains by **claiming** the daemon.

Installing the Hub is optional. Every Hub action is also a `harness` CLI command on the daemon host
([management parity](https://github.com/dflippojr/agent-harness/blob/main/docs/management-parity.md)).

## Status

The Hub includes the server, first-run claim, sign-in, **Overview**, **Settings**, and the console's layout.
The other sections (Modules, Apps, Pairing requests, Marketplace, and Accounts, skills, maintenance and
runners) each have an issue in this repository; until they ship, their pages list the equivalent CLI commands.

Settings renders the daemon's complete registry, grouped by category, with types, defaults, allowed values,
read-only guidance, current and pending revisions, and restart requirements. Validate shows field errors without
saving. Save sends the revision; a concurrent change is refused and the page reloads the current settings. Rollback
and restart each require an in-page confirmation. Settings and Overview poll health during a restart and report
last-good recovery or quarantined settings. Restart requires a daemon supervisor; an unsupervised daemon returns
its manual restart instructions.

Settings also manages smart approvals, backend model/effort defaults, the memory agent profile (saving commits
and pushes to its library), member GitHub access, read-only Google sign-in status, and test notifications. Secret
references remain file paths: the Hub never opens or resolves those files. Installer-only secrets remain hidden
and read-only. Every action includes its daemon CLI equivalent.

## Install and run

Requires Python 3.12+ and a running Agent Harness daemon on the same machine.

```bash
pip install git+https://github.com/dflippojr/agent-harness-hub
python -m harness_hub --daemon-url http://127.0.0.1:8100 --state-dir ~/.agent-harness-hub
```

The Hub listens on `http://127.0.0.1:8120` by default (`--host`, `--port`). The daemon installer
(`install.ps1 -WithHub`, `install.sh --with-hub`) installs and starts it for you, either from pip or as a Docker
image, and approves the claim in the same run.

Remote access is supported only through an HTTPS proxy, such as `tailscale serve`, with
`--public-url https://hub.example.ts.net`. Keep the upstream bound to loopback, or a private container network
that only the proxy can reach. Non-loopback listeners require an HTTPS public URL; this setting does not add TLS
to the upstream listener. Never expose that HTTP port directly to the LAN or internet.
A remote `--daemon-url` must use HTTPS; HTTP is accepted only for loopback daemon addresses.

Docker:

```bash
docker build -t agent-harness-hub .
docker run -d --network host --mount type=bind,src="$HOME/.agent-harness-hub",dst=/hub-state \
  --user "$(id -u):$(id -g)" agent-harness-hub --daemon-url http://127.0.0.1:8100 --state-dir /hub-state
```

| Option | Default | What |
| --- | --- | --- |
| `--daemon-url` | `http://127.0.0.1:8100` | the daemon this Hub administers |
| `--state-dir` | `~/.agent-harness-hub` | owner-only directory holding the Hub key and claim state |
| `--host`, `--port` | `127.0.0.1`, `8120` | where the Hub listens |
| `--claim-file` | none | start a claim at once and write `{"id", "match_code"}` there (the daemon installer reads it) |
| `--public-url` | none | the HTTPS URL browsers use, such as a `tailscale serve` address; allowed as a Host and makes cookies `Secure` |
| `--allowed-host` | none | another exact `Host` header to answer (repeatable) |
| `--name` | `Hub` | the name the daemon records for this Hub |

## Sign in

The Hub holds an admin-equivalent key, so only the owner may use it. On the daemon host, as the account that runs the
Hub, print a one-time sign-in link and open it:

```bash
python -m harness_hub signin-link --state-dir ~/.agent-harness-hub
# Docker: docker exec <hub container> python -m harness_hub signin-link --state-dir /hub-state
```

The link works once, for ten minutes. Its code travels in the URL fragment, which the browser never sends to a server
or puts in a Referer; the page posts it once and gets an HttpOnly, SameSite=Strict session cookie for twelve hours.
The cookie alone cannot authorize requests: a second per-session value is kept in origin-scoped `sessionStorage`
and sent in `X-Hub-Session`. Each new tab needs its own sign-in link. HTTPS uses a `__Host-hub_session` cookie
with `Secure`, `HttpOnly`, `SameSite=Strict` and no Domain attribute.
Proving you can read the Hub's owner-only state directory is the same proof the daemon uses for local callers.
Sign-in from the owner's tailnet identity is tracked separately.

## First run: claiming the daemon

A daemon has at most one Hub ([agent-harness #543](https://github.com/dflippojr/agent-harness/issues/543)).

1. Open the Hub and choose **Start claim** (the installer's `--claim-file` start does this by itself). The Hub asks
   the daemon for a Hub claim with a PKCE challenge and shows the six-digit **match code**.
2. On the daemon host, check that the code matches and run the command the Hub shows:
   `harness hub approve <request id> --match <code>`. Only the host can approve: the CLI reads a secret from the
   daemon's data directory.
3. The Hub redeems the claim with its verifier and keeps the `hub` key in its state directory. Overview then shows the
   daemon's version, health, capabilities and that this Hub is the claimed one.

When the daemon already has a Hub, the claim is refused (409) and the Hub shows how to release the old one on the host:
`harness hub release --confirm`. An expired or denied claim shows that, with a button to start a new one. If the Hub
is released later, the Hub notices that its key no longer works, forgets it and offers a new claim.

## Modules

Modules shows each daemon module's running presence, saved switch and scalar status detail, including status errors.
Switches come from the settings schema and save with the current revision. A pending switch is labelled until the
daemon restarts; **Restart in Settings** opens the existing confirmation and health recovery flow.

When inventory is disabled, Modules offers its registry switch. If the package is absent, the page points to the
daemon installer's `--enable-modules` option. Inventory errors, including a named entries-file error, appear beside
the last good snapshot and its timestamp. The complete snapshot is kept in memory and in private `inventory.json`,
bound to the daemon URL and this Hub's key ID; future Apps and Marketplace views can reuse it. Only allowlisted
module fields are sent to the browser.

## Security rules

- The Hub's server holds the `hub` key, the claim's PKCE verifier and the sign-in codes' hashes in its owner-only
  state directory. The browser never receives the `hub` key, any `ha-`/`ho-`/`hp-` token or prefix, a PKCE verifier
  or a provider credential: responses are built field by field from allowlists, and a guard withholds (HTTP 500) any
  `/api/` response that still contains one. Tests check every response in the claim flow, against a fake daemon and
  a real one.
- The Hub never reads an App's token response and never claims or redeems an App's pairing request: App tokens go
  from the daemon straight to the App ([agent-harness #519](https://github.com/dflippojr/agent-harness/issues/519)).
  The only token response it reads is its own Hub claim's, and it keeps the key only if the daemon says its role is
  `hub`.
- Browser writes need an `X-Hub-Request` header (a CORS preflight this server never grants) and a same-origin
  `Origin`; unknown `Host` headers are refused (DNS rebinding); pages are served with a strict Content Security Policy
  and no inline script.
- The Hub binds to loopback by default. Reach it from other devices the way you reach the daemon, for example with
  `tailscale serve` and an HTTPS `--public-url`. Do not publish the HTTP upstream port on the LAN.
- Windows state gets a protected DACL granting access only to the current user and SYSTEM, including existing
  state files. Failure to set those permissions prevents startup or sign-in code creation.
- A 401 must be confirmed with another owner API call before the Hub forgets its persistent key. This prevents
  a transient rejection from destroying it. Local HTTP does not authenticate the daemon: a process squatting on
  its port can still receive credentials. Daemon identity pinning or protected local IPC requires upstream
  Agent Harness support; use an authenticated HTTPS daemon endpoint when this threat matters.

## Develop

```bash
python -m pip install --require-hashes -r requirements-dev.lock -r requirements-build.lock
python -m pip install --no-deps --no-build-isolation -e .
ruff check .
node --test tests/browser-session.test.mjs
pytest                     # unit tests against a fake daemon
HUB_E2E_DAEMON_SRC=../agent-harness pytest -m e2e   # a real daemon from a checkout (its requirements installed)
```

The UI is plain ES modules in `harness_hub/static` with no build step. Every tap target is at least 44 px, and the
layout switches from a phone menu to a left rail at 900 px.
The Modules browser flow can also be run with `playwright-cli run-code --filename=tests/modules-browser.js`
on a signed-in Modules page backed by a fresh `tests.conftest.FakeDaemon` (one browser session).

Dependency versions and artifact hashes are reviewed in `requirements*.lock`; CI also pins the daemon fixture,
Actions commits, and the Docker base digest. Update locks deliberately with uv:

```bash
uv pip compile requirements-build.in --universal --python-version 3.12 --generate-hashes -o requirements-build.lock
uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.lock
uv pip compile pyproject.toml requirements-daemon.in --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-e2e.lock
```

Update exact versions in `pyproject.toml` and the daemon commit/requirements snapshot together when needed.

### Apps

Apps shows the daemon inventory's connection state, last seen, scopes, origins,
catalog identity, token age (warning at 90 days; no automatic expiry), aggregate
sessions, usage and errors. Installed entries use the inventory's paired ids,
including its origin checks, rather than matching display names. The complete
inventory stays in the owner-only cache shared with Modules; a failed refresh
shows the last good snapshot and disables management until a fresh read succeeds.

App names are labeled as App-supplied; duplicate names and the reserved "This Hub"
label show warnings. Revoke is offered only for App and device keys. Its confirmation shows
the key id, origins, catalog id, creation time and verified-entry pairing. Owner
and runner keys are managed on the host, without App erasure or restore hints. This Hub
has no revoke control; release it on the host with `harness hub release --confirm`.
The erasure queue shows deadlines. Restore stays on the daemon host: run the exact
`harness apps restore <app_id>` command shown for the revoked App before erasure.
The CLI returns a replacement credential once; reconnect the App using that
credential. The Hub never calls the restore route or receives an App token.
Restore remains CLI-only by the owner decision in agent-harness#597.

The App detail view supports running/queued session caps (blank restores daemon
defaults) and retention (blank keeps sessions until deleted). Saving retention
requires confirmation because shortening it can erase older sessions during
cleanup. All controls show their supported host CLI equivalents.

Token-shaped App display fields are replaced with `[withheld: token-shaped]` so
an App name cannot block inventory or revoke. Exact Hub keys and claim verifiers
still withhold the whole affected view; Apps and Modules are checked separately.
Malformed erasure or session-cap replies show errors instead of an empty queue or
editable default caps.

With Playwright installed locally, run the isolated Apps security regressions:
`npx --no-install playwright test --config=tests/playwright.config.cjs --workers=1`.
These specs intercept all network requests and start no servers.
