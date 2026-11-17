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

A case whose state the disposable daemon cannot produce (a skill proposal from an agent session, a connected Mac runner,
Remote Control folders) calls `daemon_cannot(target, reason)`: it runs on the fake and is skipped on the daemon with
the reason. Keep these to what truly cannot be produced.

## What keeps it whole

- `tests/contract/test_coverage.py` fails when a fake answers a route with no contract entry (the two tripwires the Hub
  must never call excepted), when the Hub can call a route the contract lacks or the contract lists one the Hub never
  calls, or when a contract endpoint has no case.
- The fake daemon checks every request the Hub sends during the unit suite against the contract's request shapes
  (`tests/conftest.py`), and a test whose Hub sent one outside it fails.
- `tests/contract/test_views_allowlist.py` holds the browser allowlists in `harness_hub/views.py` (`views.SCHEMAS`) to
  the reply shapes here (#252). Each name a view passes on must be a field of a reply that view serves, or sit in its
  `ALLOWED_EXTRA` with a reason, so a view can no longer wait for a name the daemon never sends (#88). A reply
  described as `MapOf(ANY)` names no fields, and the view beneath it is not checked.

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
