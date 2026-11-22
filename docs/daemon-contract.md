# The daemon contract

The Hub's core promise is administering a real Agent Harness daemon correctly. Most Hub tests run against hand-written
fakes (`tests/*_fake.py`, wired up in `tests/conftest.py`), and a fake that shares the Hub's wrong idea of the daemon
passes every test: in #88 the Hub read `at`/`timestamp`, the daemon sends `ts`, and the fake sent what the Hub read.
The contract suite in `tests/contract/` makes a real daemon the judge of every fake.

## What it holds

`tests/contract/endpoints.py` lists every daemon endpoint the Hub calls, keyed `"METHOD /path"` with the Hub's own
path parameter names (`harness_hub/daemon.py`). For each one it gives:

- the request body the Hub sends (`None`: no body) and, for reads that take one, the query fields;
- the status codes the daemon answers, each with its response shape;
- the error body every refusal answers: `{"detail": ...}`, plus the daemon's `error.code` when it has one.

A field is *required* when the daemon always sends it and the Hub reads it, and *optional* when the Hub reads it but
the daemon sends it only sometimes. Extra fields are allowed: the daemon sends more than the Hub reads, and the fakes
add private fields the Hub must never pass through. `tests/contract/shapes.py` is the small shape language.

## Two targets

Every case in `tests/contract/test_*_contract.py` runs on two targets (`tests/contract/targets.py`):

| Target | Test id | Where it runs | What it proves |
| --- | --- | --- | --- |
| The in-repo fake daemon | `[fake]` | `pytest` (the unit suite, CI `test` job) | the fake conforms |
| A disposable real daemon | `[daemon]`, marked `e2e` | `HUB_E2E_DAEMON_SRC=<checkout> pytest tests/contract -m e2e` (CI `e2e` job at the pin, `drift.yml` nightly at main) | the daemon still conforms |

The real daemon runs from an agent-harness checkout in fake model mode, on loopback, with throwaway config and data and
HOME inside its own directory (as `scripts/ui_instance.py` starts the UI journeys' daemon). Backups, instruction skills,
notifications (to a loopback ntfy sink) and a memory library (cloned from a bare repo in its directory) are switched
on, so their routes answer for real. Images and Remote Control need ComfyUI or Claude Code: their cases check the
daemon's absent answer instead. The Hub claims it exactly as `claim.py` does; the owner's approval is the daemon's own
CLI, `harness hub approve`.

The cases call the daemon with their own HTTP client. The Hub's client also checks, on each new connection to a
loopback daemon, that the listener belongs to the daemon's account before it sends anything (`harness_hub/endpoint.py`,
README "Daemon connection"); `tests/e2e/test_real_daemon.py` proves that check against the same real daemon, which
runs as the test's account.

A case whose state the disposable daemon cannot produce (a skill proposal from an agent session, a connected Mac runner,
Remote Control folders) calls `daemon_cannot(target, reason)`: it runs on the fake and is skipped on the daemon with
the reason. Keep these to what truly cannot be produced.

## What keeps it whole

- `tests/contract/test_coverage.py` fails when a fake answers a route with no contract entry (the two tripwires the Hub
  must never call excepted), when the Hub can call a route the contract lacks or the contract lists one the Hub never
  calls, or when a contract endpoint has no case.
- The fake daemon checks every request the Hub sends during the unit suite against the contract's request shapes
  (`tests/conftest.py`), and a test whose Hub sent one outside it fails.
- `tests/contract/test_views_allowlist.py` holds the browser allowlists in `harness_hub/views/` (`views.SCHEMAS`) to
  the reply shapes here (#252). Each name a view passes on must be a field of a reply that view serves, or sit in its
  `ALLOWED_EXTRA` with a reason, so a view can no longer wait for a name the daemon never sends (#88). A reply
  described as `MapOf(ANY)` names no fields, and the view beneath it is not checked.

## The daemon's OpenAPI document

A `[daemon]` case proves only the states a disposable daemon reaches: a reply it cannot produce (`daemon_cannot`), or
a field it sends only sometimes, is never compared. agent-harness publishes `tools/openapi/openapi.json` with a
documented model per route (agent-harness#1017), so `tests/contract/test_openapi_subset.py` (marked `e2e`, #269)
holds every field the contract lists to that document in the same checkout: request fields, query fields and the
fields of each success reply, at any depth, by name (`tests/contract/openapi.py`). Types, nullability and
required-ness stay with the `[daemon]` cases. A contract field the document lacks fails, named by endpoint and path.
When it shows the case first did (#269 found GitHub member sign-in's preflight and the skill export bundle wrong),
correct the contract from the daemon's code; when the document is what is wrong, report it on agent-harness. Never
drop the check.

Two things are listed, not failed: a path whose replies are still undocumented (an empty schema), and a place inside a
documented reply that the document leaves open (an object with no properties) while the contract names fields beneath
it. Each shows as an `UncheckedContractField` warning in the run's summary. Error replies are not compared: the
document has only FastAPI's 422 there. The walk branches on `anyOf` and `oneOf` but does not compare `allOf`: one
anywhere it reaches fails the endpoint, so the check never guesses. The daemon's document has none, and an e2e case
pins that; when it gains one, teach `openapi.py` to compare it. Unit cases in the same file pin the walk on a synthetic document, and one
`[daemon]` case takes fields out of the real document to show each kind of miss fails.

## Host commands

Every Hub action has a `harness` command on the daemon host, and the pages print it (README, "CLI parity"). The daemon
is the judge of those lines too: `tests/contract/test_host_commands.py` (`[daemon]`, marked `e2e`, #251) hands every
command the Hub can print to the daemon's own CLI parser (`harness/cli.py` `_build_parser`) in a subprocess from the
checkout, and runs none of them. The commands come from:

- the server: the administration catalogue (`administration.OPERATIONS`), its not-offered rows
  (`routes/settings.py`), and `claim.APPROVE_COMMAND` and `RELEASE_COMMAND`;
- the pages: the one table `harness_hub/static/lib/commands.mjs`, read through node. A page prints a command with
  `hostCommand(name, values)`; `<name>` is a word the owner or the page fills in, and `[flag: text]` an optional part,
  so the check parses each line with and without it.

Each placeholder becomes exactly one word, so a missing or extra argument still fails. An unknown subcommand, a wrong
number of positionals, an unknown flag, an abbreviated flag (the check turns argparse's abbreviations off) or a
placeholder that fills another argument's place (a swapped positional; `ALIASES` in `tests/contract/host_commands.py`
names the few that differ from the daemon's argument names) fails the case, named by its source. A placeholder that
fills a typed argument takes a value of that type from `TYPED`.

`tests/test_host_command_sources.py` keeps the list whole: no static file but `commands.mjs` writes a `harness`
command, every table entry is printed by a page, and no string in `harness_hub` prints a command the check does not
read. What the parser cannot see: a flag it treats as optional that the daemon requires at run time, as
`--acknowledge-elevated` was in #91. `commands.mjs` keeps that flag's optional part next to the command, and the page's
condition decides it. If agent-harness renames `_build_parser`, compare the lines with `admin_commands()` rows instead
of skipping the check.

## Tailnet identity

`harness_hub/tailnet.py` decides when the Hub trusts the `Tailscale-*` identity headers, with the rules of the
daemon's `harness/tailscale_peer.py`, but it is a copy. `tests/contract/test_tailnet_conformance.py` (`[daemon]`,
marked `e2e`, #253) imports the daemon's module from the checkout beside the Hub's and asks both the same synthetic
questions, one decision at a time:

- which executable is tailscaled: the install directories, and the executable sitting directly in one;
- whether a connection's server address is loopback, and whether two addresses are the same;
- whether a serve-config document (`tailscale serve status --json`, parsed by each side) forwards raw TCP to the port;
- whether the one `X-Forwarded-For` source tailscaled names is another node and not this machine;
- whether a scope carries identity headers, and what removing them leaves;
- the per-connection verdict cache, and the peer verdict itself on injected facts (the connection's owner, the
  service's PID, the owner's parent and executable).

Where the two signatures differ, a small adapter per decision asks both the same question. Every difference either
side intends is listed in the test's `KNOWN` table with its reason: the Hub refusing malformed input the daemon skips
(fail closed), and the daemon accepting the `tailscaled.exe /subproc` child of the Tailscale service, which the Hub
has not followed. Any other difference fails, named by decision and case; so does a listed one that no longer differs,
so the table stays true. Nothing looks up a process, lists interfaces or runs the CLI (psutil and `subprocess.run`
answer synthetically), so it runs on Linux as well as Windows, and the daemon's module may not look anything up at
import time. Out of scope, being state rather than a decision: the daemon's lasting refusal once it has seen a raw
forward, its cache of `tailscale ip`, and the Hub's fresh lookups for sign-in.

When it fails nightly, the daemon changed a rule: decide whether the Hub follows (change `tailnet.py`) or the
difference is intended (add it to `KNOWN` with the reason). A daemon refactor that renames a function breaks an
adapter the same way; fix the adapter.

## When it fails

- **`[fake]` fails**: the fake drifted from the contract. Fix the fake to answer what the daemon answers; if a Hub test
  then fails, the Hub shared the fake's mistake.
- **`[daemon]` fails at the pin**: the contract is wrong. Correct it from the daemon's answer, then the fake.
- **`[daemon]` fails nightly at main**: the daemon changed. `drift.yml` opens a `bug` issue naming the failing cases
  and the commits since the last green. Decide whether the Hub must follow the daemon (contract, fakes, Hub) or the
  daemon broke the Hub (report it on agent-harness).

## The pin and the nightly drift run

CI checks the daemon out at one pinned commit (`ref:` in `.github/workflows/ci.yml`, e2e and ui jobs). Nightly,
`.github/workflows/drift.yml` runs the contract suite, the e2e tests and the real-daemon UI journeys against
agent-harness main HEAD on GitHub-hosted runners, then `scripts/drift_report.py`, with the default `GITHUB_TOKEN`:

- **green**: opens or refreshes one issue, "Bump agent-harness pin to &lt;sha&gt;" (labels `ready`, `tier:mechanical`,
  `verification-uplift`), with the run link, and closes an open drift bug;
- **red**: opens or refreshes one `bug` issue naming the failing cases and the agent-harness commit range since the
  last green (the newest bump issue's sha, else the pin);
- **pin older than 14 days** (the limit in AGENTS.md) while main has moved past it: comments on that issue once per
  pinned commit.

A pull request that changes `drift.yml` or `scripts/drift_report.py` runs the workflow as a dry run: the report prints
what it would write and writes nothing. `workflow_dispatch` runs it for real on demand.
