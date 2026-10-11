# agent-harness-hub

**Hub** is the standalone admin console for one [Agent Harness](https://github.com/dflippojr/agent-harness) daemon:
think of a home router's admin page plus marketplace access. It is its own app with its own small Python server,
running on the daemon host on its own port. It administers the daemon only through the owner API
(`/api/admin/v1`) with the daemon's exclusive `hub` key, which it obtains by **claiming** the daemon.

Installing the Hub is optional. Every Hub action is also a `harness` CLI command on the daemon host
([management parity](https://github.com/dflippojr/agent-harness/blob/main/docs/management-parity.md)).

## Status

This is the bootstrap: the server, the first-run claim, sign-in, the **Overview** section and the console's layout.
The other sections (Settings, Modules, Apps, Pairing requests, Marketplace, and Accounts, skills, maintenance and
runners) each have an issue in this repository; until they ship, their pages list the equivalent CLI commands.

## Install and run

Requires Python 3.12+ and a running Agent Harness daemon on the same machine.

```bash
pip install git+https://github.com/dflippojr/agent-harness-hub
python -m harness_hub --daemon-url http://127.0.0.1:8100 --state-dir ~/.agent-harness-hub
```

The Hub listens on `http://127.0.0.1:8120` by default (`--host`, `--port`). The daemon installer
(`install.ps1 -WithHub`, `install.sh --with-hub`) installs and starts it for you, either from pip or as a Docker
image, and approves the claim in the same run.

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
  `tailscale serve` and `--public-url`.

## Develop

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest                     # unit tests against a fake daemon
HUB_E2E_DAEMON_SRC=../agent-harness pytest -m e2e   # a real daemon from a checkout (its requirements installed)
```

The UI is plain ES modules in `harness_hub/static` with no build step. Every tap target is at least 44 px, and the
layout switches from a phone menu to a left rail at 900 px.
