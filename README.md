# agent-harness-hub

**Hub** is the standalone admin console for one [Agent Harness](https://github.com/dflippojr/agent-harness) daemon:
think of a home router's admin page plus marketplace access. It is its own app with its own small Python server,
running on the daemon host on its own port. It administers the daemon only through the owner API
(`/api/admin/v1`) with the daemon's exclusive `hub` key, which it obtains by **claiming** the daemon.

Installing the Hub is optional. Every Hub action is also a `harness` CLI command on the daemon host
([management parity](https://github.com/dflippojr/agent-harness/blob/main/docs/management-parity.md)).

## Status

The Hub includes the server, first-run claim, sign-in, **Overview**, **Settings**, **Modules**, **Apps**,
**Pairing requests**, **Marketplace** and **Accounts and maintenance**.

Settings renders the daemon's complete registry, grouped by category, with types, defaults, allowed values,
read-only guidance, current and pending revisions, and restart requirements. Validate shows field errors without
saving. Save sends the revision; a concurrent change is refused and the page reloads the current settings. Rollback
and restart each require an in-page confirmation. Settings and Overview poll health during a restart and report
last-good recovery or quarantined settings. Restart requires a daemon supervisor; an unsupervised daemon returns
its manual restart instructions.

Settings also manages smart approvals, backend model/effort defaults, the memory agent profile (saving commits
and pushes to its library), member GitHub access, read-only Google sign-in status, and test notifications. Secret
references remain file paths: the Hub never opens or resolves those files. Installer-only secrets remain hidden
and read-only. Every action has its daemon CLI equivalent behind a **Host command** button. When the daemon has no `memory_library` module
(not installed, or switched off), the memory section says so and shows how to enable it instead of an error.

## Install and run

Requires Python 3.12+ and a running Agent Harness daemon on the same machine, at
[agent-harness #606](https://github.com/dflippojr/agent-harness/pull/606), merge commit
`aa5a9047dbe4bcba6b7ec3348d0cd529c00ebe2b` (`aa5a904`), or later. This minimum daemon enforces
typed-code confirmation for browser claims as well as native claims. Overview warns when the daemon is
older than this minimum.

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
| `--trust-unverified-identity-headers` | off | **unsafe**; outside Windows, trust `Tailscale-*` headers from any local process (see [Sign in](#sign-in)) |

## Sign in

The Hub holds an admin-equivalent key, so only the owner may use it. On the daemon host, as the account that runs the
Hub, print a one-time sign-in link and open it:

```bash
python -m harness_hub signin-link --state-dir ~/.agent-harness-hub
# Docker: docker exec <hub container> python -m harness_hub signin-link --state-dir /hub-state
```

The link works once, for ten minutes. Its code travels in the URL fragment, which the browser never sends to a server
or puts in a Referer; a truncated or mangled link is refused on the page without being sent. The page posts a
well-formed code once and gets an HttpOnly, SameSite=Strict session cookie for twelve hours.
The cookie alone cannot authorize requests: a second per-session value is kept in origin-scoped `sessionStorage`
and sent in `X-Hub-Session`. Each new tab needs its own sign-in link. HTTPS uses a `__Host-hub_session` cookie
with `Secure`, `HttpOnly`, `SameSite=Strict` and no Domain attribute.
Proving you can read the Hub's owner-only state directory is the same proof the daemon uses for local callers.

### With your tailnet identity

Through `tailscale serve`, the sign-in page also offers **Continue as `<your login>`**. This is the other owner proof
the daemon accepts, with the same rules:

- `tailscale serve` adds `Tailscale-User-Login`. The Hub trusts the `Tailscale-*` headers only when the process on the
  other end of the TCP connection is the running Tailscale Windows service, and its executable is `tailscaled.exe`
  from the Tailscale install directory (`Program Files\Tailscale`). Headers from any other process are removed before
  anything reads them, including a copy of the installed `tailscaled.exe` that someone started themselves (as a
  userspace proxy, say) and a `tailscaled.exe` elsewhere. If the service is missing or stopped, or its process can't
  be looked up, no identity is trusted. Sign-in always looks the connection up again rather than reuse a verdict
  cached for an earlier connection.
- serve adds the headers to every request, but only `/api/session` reads them: `GET` to show your login, `POST` to
  sign in. So the Hub verifies them only on those two requests and removes them from every other one (pages, static
  files, the rest of the API) without a lookup.
- The node's `tailscale serve` config is part of that boundary: a raw TCP forward (`tailscale serve --tcp` or
  `--tls-terminated-tcp`) carries a client's own headers through the service unchanged. So at sign-in the Hub reads
  the serve config through the Tailscale CLI, foreground sessions and services included, and refuses tailnet sign-in
  when any TCP handler forwards to the Hub's port, or when the config can't be read. This is a best-effort check, not
  an enforced boundary: it catches only a forward still in place when the sign-in request arrives. Anything able to
  change this node's serve config (the Tailscale backend's user, local admins, a sandbox that can reach the
  tailscaled pipe) can open a connection through a raw forward and remove the forward before sending the request.
  Publish the Hub with serve's HTTPS proxy (`tailscale serve --bg --https=443 http://127.0.0.1:<port>`), never a
  raw TCP forward.
- A subnet route is the other way tailscaled passes a device's own bytes on. When this node advertises a route,
  tailscaled ends a routed TCP connection itself and opens a new one to the target from the Tailscale service's own
  socket, which no serve config shows. With the Hub's `host` set to `0.0.0.0` or a LAN address the route covers, a
  tailnet device could reach it that way with headers it wrote itself. `tailscale serve` proxies to the Hub's loopback
  listener, so the Hub trusts the identity headers only on a connection whose server address is loopback; on any
  other address, or when the address is missing, they are removed.
- tailscaled vouches for the tailnet node a request came from, and a program on this machine that opens the node's
  own serve address arrives as this node, which Tailscale resolves to the owner. So on a connection verified as
  tailscaled, and only there, the Hub reads the source address serve puts in `X-Forwarded-For` (serve replaces any
  value a client sent). The identity is ignored when that header is missing, lists more than one address, or names
  any address of this machine, its Tailscale IPs included. Showing your login reuses this machine's address list for
  up to five seconds; sign-in lists the addresses again. Tailnet sign-in therefore works only from another device;
  on the Hub's own machine, use a sign-in link. `X-Forwarded-*` and `Forwarded` headers are read nowhere else.
- The peer check is implemented for Windows only, like the daemon's. Elsewhere the headers are always removed unless
  you start the Hub with `--trust-unverified-identity-headers`, the counterpart of the daemon's
  `listen.trust_unverified_identity_headers`. That option trusts **any** local process (or container reaching the
  host loopback) that sends the headers; use it only where no untrusted code can connect to the Hub's port.
- The Hub signs the login in only if the daemon lists it in `allowed_logins` (`harness.local.yaml`), read from the
  owner API discovery root (reused for up to five seconds), compared exactly. That needs a claimed Hub and a daemon with Owner API
  1.26 or later ([agent-harness #621](https://github.com/dflippojr/agent-harness/pull/621)). An empty list
  (open-owner legacy mode) admits no tailnet login. An unclaimed Hub, an open-owner daemon and an unlisted login all
  get the same refusal; the Hub's log says which. Each login gets five tailnet attempts a minute, counted apart from
  the sign-in link throttle so no one else can lock the owner out.

The session is the same as a link's: twelve hours, an HttpOnly cookie plus a per-tab `X-Hub-Session` value. Sign-in
links keep working, and remain the only way in before the Hub has claimed its daemon.

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

Stopping a waiting claim is ordered with redemption: if an in-flight redemption succeeds, the Hub keeps its key
and reports **Claimed**. If stopping returns **Unclaimed**, that request cannot later change the Hub's claim state.

If the daemon issues the one-time Hub key but the private state write fails, Overview shows **Waiting to save key**.
The Hub keeps that credential privately in memory and retries saving it without redeeming again. It offers no
claim replacement or cancellation while saving is pending, and enables owner operations only after the save
succeeds. Keep the Hub process running and repair free space, write permissions, or the state directory's storage.
Saving retries with exponential backoff from 2 seconds to at most 60 seconds; an explicit claim action wakes the
retry and resets the delay. Storage failures log at most once a minute. Claim writes run outside the event loop,
and reads continue serving the last saved snapshot while a disk write is blocked. Terminal results such as
Denied are retained in memory and retried locally too; they never cause another redemption.

Claim actions wait at most 15 seconds for the claim lock, then return a retryable HTTP 503 with the current state.
Starting a new claim also has a 15-second total network deadline. Redemption uses the daemon client's 5-second
connect and 10-second per-phase timeouts, without a total cancellation deadline: a trickling response can last
longer, and cancelling it can lose the one-time key. Stop waiting leaves that operation intact if its wait expires.
Shutdown waits up to 15 seconds; a shielded cleanup task continues draining redemption, attempts a final save,
and only then closes the daemon client. Cancelling shutdown does not cancel that task. Cleanup requires the
process and event loop to remain running; a forced exit cannot guarantee delivery of a daemon's minted key.
If persistence still fails before the process exits (or the process crashes), the credential in memory is lost:
on the daemon host run
`harness hub release --confirm`, repair the Hub's state storage, restart the Hub, and start and approve a new claim.

Private state writes fsync the temporary file before replacement. POSIX also fsyncs the parent directory after
the rename where the filesystem supports directory fsync; other directory sync failures leave the result pending
for retry. Windows uses MoveFileExW with REPLACE_EXISTING and WRITE_THROUGH after the file fsync. Windows does not
expose the same directory-fsync guarantee, and same-volume rename metadata durability depends on the filesystem
and storage hardware; forced power loss can still require host recovery.

## Modules

Modules shows each daemon module's running presence, saved switch and scalar status detail, including status errors.
Switches come from the settings schema and save with the current revision. A pending switch is labelled until the
daemon restarts; **Restart in Settings** opens the existing confirmation and health recovery flow.

When inventory is disabled, Modules offers its registry switch. If the package is absent, the page points to the
daemon installer's `--enable-modules` option. Inventory errors, including a named entries-file error, appear beside
the last good snapshot and its timestamp. The complete snapshot is kept in memory and in private `inventory.json`,
bound to the daemon URL and this Hub's key ID; Apps and Marketplace reuse it. Only allowlisted
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
layout switches from a phone menu to a left rail at 900 px; a tap outside the open phone menu closes it.
For CLI parity, every action the Hub takes has the daemon host command that does the same. Those commands are off by
default: a **Host command** button under each action shows its command, with Copy. The **Show host commands** switch in
the rail footer (in the phone menu on a phone) shows them all in place of the buttons; the browser remembers it as
`hub.hostCommands` in localStorage, the only thing the Hub stores there. Steps only the daemon host can take, such as
approving a Hub claim, restoring an App or minting a sign-in link, always show their command.
The shell (sign-in links, the phone menu, Modules and the Settings memory section) has its own isolated spec:
`npx --no-install playwright test --config=tests/playwright.config.cjs --workers=1 shell-browser.spec.cjs`.
The Modules browser flow can also be run with `playwright-cli run-code --filename=tests/modules-browser.js`
on a signed-in Modules page backed by a fresh `tests.conftest.FakeDaemon` (one browser session).

For browser testing, `python scripts/ui_instance.py start --dir <dir>` starts a disposable Hub and a fake model
daemon from an agent-harness checkout (`HUB_UI_DAEMON_SRC`), on loopback with synthetic data, and
`npm --prefix tools/ui-tests test -- --workers=1` runs the Playwright journeys against one.
[docs/ui-testing.md](docs/ui-testing.md) is the contract: start, sign-in, journeys and teardown.
To check a change end to end (launch, doctor, drive, evidence, cleanup), agents use the
[verify skill](.claude/skills/verify/SKILL.md): `python .claude/skills/verify/scripts/verify.py run`.

Dependency versions and artifact hashes are reviewed in `requirements*.lock`; CI also pins the daemon fixture,
Actions commits, and the Docker base digest. Update locks deliberately with uv:

```bash
uv pip compile requirements-build.in --universal --python-version 3.12 --generate-hashes -o requirements-build.lock
uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.lock
uv pip compile pyproject.toml requirements-daemon.in --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-e2e.lock
```

Update exact versions in `pyproject.toml` and the daemon commit/requirements snapshot together when needed.

### Release

Pushing a `vX.Y.Z` tag runs `.github/workflows/release.yml`. The tag must equal `v` + `harness_hub.__version__`
and point at a commit on `main`. That ancestry check is an accident guard, not a control: the tag runs the workflow
file from the tagged commit, so a deliberate release is stopped only by the reviewer gate and the tag ruleset below.
The workflow builds the wheel, sdist and image once and smoke-tests the image with `scripts/smoke-image.sh`. It then
publishes the wheel and sdist to PyPI as `agent-harness-hub` through a trusted publisher (no API token) in the `pypi`
environment. In the `ghcr` environment it pushes the image as `ghcr.io/dflippojr/agent-harness-hub:X.Y.Z` with the
workflow's `GITHUB_TOKEN`, and also as `:latest` only when `vX.Y.Z` is the highest release tag on `main`, so a
backport never moves `:latest` backwards. If a run doesn't move `:latest`, it logs a warning. Last, it attests the
pushed digest's build provenance. The package is MIT licensed (`LICENSE`).

The `ghcr` environment is a reviewer prompt for this workflow only. The package permissions come from the workflow
file, not from the environment, so another workflow in this repository can still push the image. The signer check
below is what ties an image to this workflow.

These are the names for the daemon installer's `HARNESS_HUB_PACKAGE` and `HARNESS_HUB_IMAGE` (agent-harness #547).
Pin the image to a version or, better, a digest instead of `:latest`, and check the digest's provenance before you
run it. Pin the signer as well as the repository, using the tag of the version you run:

```bash
docker pull ghcr.io/dflippojr/agent-harness-hub:X.Y.Z
gh attestation verify oci://ghcr.io/dflippojr/agent-harness-hub@sha256:<digest> \
  --repo dflippojr/agent-harness-hub \
  --cert-identity "https://github.com/dflippojr/agent-harness-hub/.github/workflows/release.yml@refs/tags/vX.Y.Z" \
  --deny-self-hosted-runners
# then HARNESS_HUB_IMAGE=ghcr.io/dflippojr/agent-harness-hub@sha256:<digest>
```

Owner setup before the first tag. Only an admin can do these; the workflow doesn't create them:

1. On PyPI, register `agent-harness-hub` with a trusted publisher for repository `dflippojr/agent-harness-hub`,
   workflow `release.yml` and environment `pypi`.
2. In Settings → Environments, create `pypi` and `ghcr`. Protect both: the owner is a **required reviewer**, and
   **deployment branches and tags** are limited to the tag pattern `v*`. If a workflow names an environment that
   does not exist, GitHub creates it with no protection, so create both before the first tag.
3. In Settings → Rules → Rulesets, add a tag ruleset that targets `refs/tags/v*`. It restricts creations, updates
   and deletions, with only repository admins on the bypass list. Then agent or leaked tokens can't create, move or
   delete release tags.
4. In Settings → Rules → Rulesets, add a branch ruleset for `main` that requires a pull request before merging. The
   ancestry check above only means something when `main` can't take direct pushes.

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
cleanup. Every control has its supported host CLI equivalent behind a **Host command** button.

Token-shaped App display fields are replaced with `[withheld: token-shaped]` so
an App name cannot block inventory or revoke. Exact Hub keys and claim verifiers
still withhold the whole affected view; Apps and Modules are checked separately.
Malformed erasure or session-cap replies show errors instead of an empty queue or
editable default caps.

With Playwright installed locally, run the isolated Apps security regressions:
`npx --no-install playwright test --config=tests/playwright.config.cjs --workers=1`.
These specs intercept all network requests and start no servers.

### Pairing requests

Pairing requests lists the App pairing requests of the last day ([agent-harness #519](https://github.com/dflippojr/agent-harness/issues/519)),
newest first and grouped by state, with each one's name, scopes, catalog app id, origin (or "native app"), expiry
countdown and next step. The Hub never projects or renders an App request's match code. Every scope shows the daemon's
disclosure text exactly as given, as text, with its `standard` or `elevated` badge. The page polls every 5 seconds.
Overview shows a **Waiting for you** card, with a link here, counting the unexpired App requests that need you to
approve or confirm; it shows counts only, never the requests themselves, and leaves out Hub claims.
Started from distinguishes owner-armed slots from App-initiated requests. App-initiated origin and catalog ids are
stated by the requester, not verified; only the match code on your App's screen proves this is your App. For an armed
slot, the page explains that you set the origin and catalog id when arming it.

Approve and Confirm need the code the App is showing right now, typed into an empty field; the daemon compares it
and refuses a wrong code. If the App shows no code or an error, deny. If your App reported that the slot was already
claimed, list requests with `harness pairing-requests list`. For a `redeemed` row, revoke its `key_id` with
`harness keys revoke <key_id>`; for `approved` or `claimed`, deny it with `harness pairing-requests deny <id>`.

If this Hub process observes a browser slot jump from `armed` to `approved` or `redeemed` without handling a
successful confirmation, Marketplace and Pairing requests warn: "This daemon approved the claim without
confirmation; update the daemon." This is a bounded, in-memory polling backstop. A host CLI confirmation between
polls can also trigger it; verify the daemon meets the minimum above. The warning never includes an App match code.

Browser and native claims on armed slots both wait for typed-code confirmation before redemption. Elevated scopes (`sessions:all`, `approvals`, `remote_control`,
`memory_library`, `homelab`) need a separate acknowledgement: Approve stays disabled until it is ticked, and the
server refuses an approval without it before calling the daemon. If a name, origin or catalog id is withheld,
Approve and Confirm are unavailable; review the request with `harness pairing-requests list` on the host. Deny
remains available. Deny and Withdraw ask for confirmation in the page.
A request that changed state (409) or no longer exists (404) refreshes the list with a short note. Hub claims are
read-only and show `harness hub approve <id> --match <code>`, because they are decided only on the daemon host.

Approval changes the request's state only: the App then redeems its own token from the daemon with its PKCE
verifier, so the Hub never calls the App-side `claim` or `token` routes and never sees an App token. Every action
has its `harness pairing-requests` CLI equivalent behind a **Host command** button. Arming slots for Hub entries belongs to [Marketplace](#marketplace).
Run the isolated browser regressions with Playwright installed locally:
`npx --no-install playwright test --config=tests/playwright.config.cjs --workers=1 pairing-security.spec.cjs`.

### Marketplace

Marketplace lists the owner's local Hub entries ([agent-harness #520](https://github.com/dflippojr/agent-harness/issues/520))
from the daemon's `GET /hub`, under the line **Being listed here grants nothing.** Each entry shows the manifest's app
name, publisher, summary and version, its declared scopes and browser origins, "Local entry, not verified", and its
state: **Paired** (with the matching key ids) or **Not paired**, with **Pending** beside it when applicable.
A pending request without an armed slot names a requester-chosen catalog id and is explicitly unverified.
Native keys and requests that match only the catalog id are listed separately under "Native keys and requests (origin not verified)", as the daemon reports
them under `unverified_origin`. Only display fields reach the browser: the manifest's URLs, e-mail addresses and
review evidence do not, and nothing named by an entry is ever fetched. An icon is shown only when it is an inline
`data:` PNG, JPEG, GIF or WebP image; anything else becomes a letter.

**Set up** opens a consent screen before anything is armed: the scopes the app will get, each with its `standard` or
`elevated` badge, the daemon's disclosure text and the publisher's stated reason; the exact origin a browser app's
slot is bound to (chosen from the entry's origins when it lists several); and a separate acknowledgement for elevated
scopes and any scope without Hub disclosure text. Initially the disclosure text is the Hub's copy of the daemon's
(agent-harness `harness/pairing_requests.py`). Every successful arm reply is checked for identical scope, tier and
text. A difference, including a blank Hub description for an add-on scope, immediately withdraws the new slot and
returns 409 without its id. The consent screen reopens with the daemon's text and a new digest; the owner must
approve again. Marketplace and Pairing requests wait for that check to finish before publishing slots, including
when another browser tab polls during withdrawal. A newer daemon's acknowledgement requirement also reopens
consent with a separate checkbox.
Unsafe or incomplete disclosures never become a consent screen, and a failed withdrawal reports an error instead
of a hand-off. CI compares the daemon's entire DISCLOSURES and ELEVATED tables with the Hub's.
The owner-API disclosure preview follow-up is [agent-harness #604](https://github.com/dflippojr/agent-harness/issues/604);
once it exists the Hub will drop SCOPE_DISCLOSURES and show the daemon's preview before arming.

Only **Approve** calls the daemon's `POST /pairing-requests`. The server re-reads the entries first, compares a
SHA-256 digest of the displayed entry (excluding live slots), and validates scopes and normalized origins against
the raw daemon entry. An entry whose binding fields would be truncated or redacted is withheld entirely. Publisher
text has Unicode control and format characters removed before display caps and before the name reaches the daemon.
The consent heading includes the catalog id beside the name.

After arming, the page shows the request id and the daemon URL as copyable text, with "Give this only to <app>; it
lets that app claim the slot once." No secret exists then: the app claims the slot with its own PKCE challenge and
fetches its own token from the daemon. Arming grants scope consent; both browser and native claims then wait for
**Confirm** with the code the app is showing right now, typed into an empty field. Browser claims and redemption
also require the bound exact origin, which alone cannot prove the claimant's identity because native clients can
forge that header. As on Pairing requests, the Hub never shows an App's code, and the daemon refuses a wrong one.
If the app shows no code or an error, withdraw the slot. If it reports the slot was already claimed, list requests
with `harness pairing-requests list`; revoke a redeemed row's `key_id` with `harness keys revoke <key_id>`, or deny
an approved or claimed row with `harness pairing-requests deny <id>`.
**Withdraw** ends a slot after an in-page confirmation. Every action has its CLI equivalent behind a **Host command** button
(`harness hub status`, `harness hub entries init|validate`, `harness pairing-requests arm|confirm|deny`).

A stale or unreadable entries file makes `GET /hub` answer 400 with a named error (`hub.entries: invalid JSON`,
`entries[0].app.app_id: duplicate id`, ...). The page shows that error with the last good entries from the shared
inventory snapshot, never an empty list and never entries the daemon did not validate, and setting up is refused
until the file is fixed. Entries that do not match the contract are withheld and counted.
Run the isolated browser regressions with Playwright installed locally:
`npx --no-install playwright test --config=tests/playwright.config.cjs --workers=1 marketplace-security.spec.cjs`.

### Accounts and maintenance

The six areas manage household accounts, name-only provider assignments, skills and proposals, maintenance,
runners/projects/folder discovery, and read-only audit logs. Each action has its host CLI equivalent behind a
**Host command** button.
Choose an area to read its current state, then choose an action or a record's control. Writes are explicit;
destructive changes show the target and edited fields in an in-page confirmation.

Google link codes appear only in the creation response, disappear on dismissal, expiry, or navigation, and are
never cached or stored by the Hub. Provider assignments accept opaque secret-file names; configure the files
on the daemon host. Skill proposals, diffs and reference files render as text. Proposal review includes the source
session, examples, metadata, SKILL.md and reference files. Installs send the exact reviewed hash; incomplete,
truncated or redacted bundles require review on the host CLI. Image retention requires a fresh preview and the
daemon's phrase typed exactly by the owner.
An empty skill project allowlist means no project receives it automatically. Allowlist changes show a fresh before
and after list for confirmation. Token-shaped display fields are withheld individually; unsafe identifiers lose
their row actions, and unaffected records stay usable.
Audit supports exclusive row-id paging and exact filters. Folder trust opens an interactive prompt on the daemon
host; discovery is available only where the daemon supports it. Owner-key and Mac runner pairing-code creation
remain host CLI actions because the Hub key cannot mint owner keys.

The checked-in administration route snapshot comes from the daemon's API/parity documentation; tests fail if
an operation loses its catalogue entry or daemon dispatch. Tests use synthetic account and folder data only.
Run the isolated administration browser regressions with Playwright installed locally:
`npx --no-install playwright test --config=tests/playwright.config.cjs --workers=1 admin-security.spec.cjs`.
