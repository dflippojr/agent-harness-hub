---
name: verify-agent-harness-hub
description: Prove a change works in the real Hub console, not only in unit tests. Launches a disposable Hub and fake model daemon on synthetic data, runs doctor checks, drives the main path (or one feature) in a browser, saves screenshots, logs and a summary, and cleans up every process it started. Use for "verify this change", "launch and check", "does it work in the app", "smoke test" in agent-harness-hub.
---

# Verify agent-harness-hub

One command runs all five steps and always cleans up:

```
python .claude/skills/verify/scripts/verify.py run [--feature <name> ...]
```

It prints `PASS`/`FAIL` per step and the path of `summary.md`, and exits non-zero if any step failed. Use
`--feature` (repeatable) to drive only what you changed; see the feature map. Never point anything here at the live
Hub, `:8100`, tailnet sign-in or a `*.ts.net` URL.

**Before the first run**, set up the tools and the daemon checkout as [docs/ui-testing.md](../../../docs/ui-testing.md)
"Scope and isolation" → **Tools** says, and the Playwright package as "Playwright and CI" → **Commands** says
(`npm ci` and Chromium are enough here). Run with that Python 3.12+ (`python` must have the Hub and daemon
requirements, including `psutil`) and with `HUB_UI_DAEMON_SRC` set.

## 1. Launch

`verify.py` runs the contract's **Start** command (docs/ui-testing.md, "Start") as its own background process, with
a fresh `--dir` under the system temp directory and `--log-dir` inside the evidence folder (so the logs survive
Teardown, as "Start" → **Logs** explains). Before waiting for anything it writes `launch.json` in the evidence folder:
the launcher's pid and start time, the instance directory and the log path. When the `READY` line arrives it adds
the Hub and daemon URLs and ports. Every process a later step starts (doctor, npm, node, browsers) is appended there
too, with its start time.

## 2. Doctor

```
python .claude/skills/verify/scripts/doctor.py --dir <instance-dir> [--expect-port <hub port>]
```

`verify.py` runs it after READY and saves its output as `doctor.txt`. One line per check, exit 1 on any failure:

| Check | Passes when |
| --- | --- |
| `ready` | `instance.json` says `ready` (the contract's readiness signal) and Hub `/healthz` and daemon `/health` answer 200 |
| `loopback` | both URLs are `127.0.0.1`, neither is a live port, the Hub is on the expected port, and every socket the recorded processes listen on is bound to `127.0.0.1` |
| `isolation` | the recorded Hub and daemon processes carry no credential variables, and their `HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`HARNESS_CONFIG_DIR`, `--state-dir`/`--config-dir`/`--claim-file` and the daemon's `data_dir`/`repos_dir` are inside the instance directory; the fake model marker is present |
| `seed` | the Hub is claimed and the seed rows from docs/ui-testing.md "Seed and sign-in" are listed |

The doctor reads; it never starts or changes anything (it mints one sign-in link to read the seed).

## 3. Drive

Playwright, Chromium only, one worker, a desktop (1440×900) and a phone (Pixel 7) project:
[`tools/ui-tests/verify/drive.spec.mjs`](../../../tools/ui-tests/verify/drive.spec.mjs) with
[`tools/ui-tests/verify.config.mjs`](../../../tools/ui-tests/verify.config.mjs). It lives in `tools/ui-tests` so it
reuses that package's pinned Playwright and the suite's `signedIn`, `openSection` and `errors` fixtures; the main
suite's config does not pick it up. There is no global setup: it drives the instance from step 1. Output: `drive.txt`.

### Feature map

Sign-in and readiness follow docs/ui-testing.md "Seed and sign-in" and the Journeys table's ready column.

| Feature (`--feature`) | Route | How it is driven | What proves it |
| --- | --- | --- | --- |
| `signin` | `#signin=<code>` → `#/overview` | a fresh `signin-link`, opened in a new tab | URL becomes `#/overview`, no sign-in card |
| `overview` | `#/overview` | open | heading "Daemon" |
| `apps` | `#/apps` | open | heading "Paired apps and keys" |
| `modules` | `#/modules` | open | heading "Module switches" |
| `pairing` | `#/pairing` | open | heading "Paired (n)" |
| `settings` | `#/settings` | Maximum turns +1, Validate, Save, reload; Roll back → Confirm rollback, reload (HUB-04's save and rollback) | "Saved.", the new value after reload, then "Rolled back to revision" and the original value after reload |

Every test also asserts no page or console errors (the suite's known exceptions apply). To cover something the map
lacks, add a tagged test to `drive.spec.mjs` and a row here; deeper journeys belong in `tools/ui-tests/tests`.

## 4. Evidence

Under `.verify/<UTC timestamp>/` (git-ignored; `--evidence <dir>` overrides):

- `summary.md`: per-step result, the loopback URLs, the screenshot list and log excerpts (the fake model banner and
  any `ERROR` or `Traceback` lines). Read it first; quote it in the PR.
- `screenshots/<desktop|phone>-<feature>.png`, plus `-settings-edited` before the rollback.
- `doctor.txt`, `drive.txt`, `launcher.txt`, `cleanup.txt`, `launch.json`, `logs/daemon.log` and `logs/hub.log`,
  and Playwright traces under `playwright/` when a test failed.

Keep the instance's Hub key, sign-in links and App key out of anything you post (docs/ui-testing.md, **Secrets**).
They die with the instance.

## 5. Cleanup

Runs in a `finally`, so also after a failed launch, doctor or drive:

1. The contract's **Teardown** (docs/ui-testing.md, "Teardown") on the instance directory.
2. Every process in `launch.json` that still runs with its recorded start time is killed, by pid. Nothing is ever
   found or killed by name or port.
3. The contract's leftover checks: no recorded process alive, the Hub and daemon ports refuse connections, the
   instance directory is gone. Any leftover fails the step and is listed in `cleanup.txt`.

If a run was killed before its own cleanup, repeat it from the evidence folder:

```
python .claude/skills/verify/scripts/verify.py cleanup --evidence .verify/<timestamp>
```

## Tests

`tests/test_verify_skill.py`: the doctor's verdicts (healthy, not ready, non-loopback, missing seed, leaked
credentials or paths) and a full `run` with a stand-in launcher whose drive step fails, checking that cleanup still
leaves no process, port or directory behind.
