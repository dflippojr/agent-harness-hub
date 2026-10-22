# Agent notes

## How to prove a change works

CI (`.github/workflows/ci.yml`) runs the jobs below on GitHub-hosted runners. The `gate` job needs all of them but
`sonar`, always runs, and fails unless each one succeeded. The ruleset requires it and `gate-guard`, which
`.github/workflows/gate-guard-target.yml` posts from `main` under `pull_request_target`, so a pull request can't
rewrite its own judge. A push after the owner's `gate-change-approved` label makes the label stale. Run the jobs your
change touches locally before you push, from the repository root, with Python 3.12 (3.10 cannot import the tests).

**Setup.** `python -m pip install --require-hashes -r requirements-dev.lock -r requirements-build.lock`, then
`python -m pip install --no-deps --no-build-isolation -e .`. The e2e and ui jobs, and the verify skill, need
`requirements-e2e.lock` in place of `requirements-dev.lock`, and an agent-harness checkout at the commit CI pins:

```
git clone https://github.com/dflippojr/agent-harness ../agent-harness
git -C ../agent-harness checkout 3d3b806a4445254423af52d753171bc497ca1be9
```

Name it with `HUB_E2E_DAEMON_SRC` (e2e) and `HUB_UI_DAEMON_SRC` (ui and verify). The pin is the `ref:` in ci.yml's
e2e and ui jobs; a later commit also works (docs/ui-testing.md, "Scope and isolation").

| CI job | Local command |
| --- | --- |
| `lint` | `ruff check .`, then `node --check` on every `harness_hub/static/**/*.mjs`, then `node --test tests/browser-session.test.mjs` |
| `test` (Linux and Windows) | `python -m pytest -q --cov=harness_hub --cov-report=term --cov-fail-under=92` (fails under 92% line coverage) |
| `e2e` (Linux and Windows) | `HUB_E2E_DAEMON_SRC=../agent-harness python -m pytest -m e2e -v -p no:cacheprovider` |
| `ui` | `npm --prefix tools/ui-tests ci`, `npm --prefix tools/ui-tests exec -- playwright install chromium webkit`, then `HUB_UI_DAEMON_SRC=../agent-harness npm --prefix tools/ui-tests test -- --workers=1` |
| `package` | `python -m build --no-isolation`, install `dist/*.whl` into a fresh venv and run `python -m harness_hub --version` there, then `docker build -t agent-harness-hub:ci .` and `scripts/smoke-image.sh agent-harness-hub:ci` (needs a running Docker; never start one yourself) |
| `pin-age` | `python scripts/drift_report.py pin-age --mode fail` (needs `gh` signed in; a pull request runs `--mode warn`) |
| `sonar` | Nothing local: SonarCloud analyses the Linux `test` job's coverage, and its quality gate is its own check |

Keep test parallelism at 4 workers or fewer. The commands are the same in PowerShell, except that environment
variables are set first (`$env:HUB_UI_DAEMON_SRC = "..\agent-harness"`).

**In the real console.** To prove a change works in the Hub console, not only in tests, use the verify skill:
[.claude/skills/verify/SKILL.md](.claude/skills/verify/SKILL.md), `python .claude/skills/verify/scripts/verify.py run
[--feature <name> ...]`. It builds on [docs/ui-testing.md](docs/ui-testing.md): a disposable Hub and fake model daemon
on loopback with synthetic data, doctor checks, a browser drive, screenshots and a `summary.md`, and full cleanup.

**The daemon contract.** The fakes and the real daemon are held to one contract:
[docs/daemon-contract.md](docs/daemon-contract.md). A change to how the Hub calls the daemon changes
`tests/contract/endpoints.py`, a case, and the fake, together; run the `[daemon]` cases with
`HUB_E2E_DAEMON_SRC=<agent-harness checkout> pytest tests/contract -m e2e`.

The agent-harness pin (`ref:` in `.github/workflows/ci.yml`) may be at most **14 days** old while agent-harness main
has moved past it. The nightly `drift.yml` run opens a "Bump agent-harness pin to <sha>" issue when main passes, a
`bug` issue when it fails, and comments when the pin is older than that. `scripts/drift_report.py pin-age` enforces
the rule: ci.yml's `pin-age` job warns on a pull request and never fails it, fails on a push to main (and `gate` with
it), and the nightly `drift.yml` run fails too. Each names the open bump issue; bump the pin to clear it.

**In the PR.** The PR body carries a `## Proof` table (the rule from dflippojr/.github#13), one row per acceptance
criterion of the issue:

```
## Proof

| Acceptance criterion | How verified (command, test name, verify skill) | Evidence (output excerpt, artifact link, red-before/green-after) |
| --- | --- | --- |
| ... | ... | ... |

### Not verified
What was not checked, and why.

### Gate changes
Tests deleted or skipped, or config loosened, with the reason. Empty if none.
```

Every criterion needs evidence: a command's output, a run or artifact link, or a test that fails before the change
and passes after. A criterion you could not check goes under **Not verified**, not in the table.
