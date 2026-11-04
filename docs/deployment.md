# Deploying the Hub on the tower

The Hub runs on the tower next to the agent-harness daemon, as a service, so real use produces real signals
([production-signals.md](production-signals.md)). The scripts are in `ops/hub/`. The tower runs Linux, so the
[Linux](#linux-systemd-user-unit) path, a systemd user unit, comes first. The [Windows](#windows-logon-task) path, a logon
task, is kept for a Windows daemon host. Both use the same layout and records, and are modelled on agent-harness
`ops/harness/`.

Commands below use placeholders for values that differ from one machine to another. `<checkout>` is the clone of this
repository on the tower, `<deploy-dir>` is the install folder, `<python>` is a Python 3.12 or later interpreter, and
`<tailnet-hostname>` is the tower's name on your tailnet. Fill each one in with your own value. This repository is
public: a host name, a tailnet name or a `--public-url` value goes in the host's own files (`hub.env` on Linux, the
task's arguments on Windows), never in a commit.

**Rules**

- **Loopback or tailnet only.** The Hub listens on `127.0.0.1:8120` and nothing else (`hub-common.sh` and
  `hub-common.ps1` pin the host; there is no switch for another). A browser on the tailnet reaches it through
  `tailscale serve`'s HTTPS proxy. Never `--host 0.0.0.0`, never a LAN address, never `tailscale funnel`.
- **No secrets in the repo.** Everything secret stays in `<deploy-dir>/state` (owner-only): the Hub key and sign-in
  codes. The scripts, `deployed.json` and `status.json` hold none.
- **The daemon is never touched.** The only unit or task the scripts name is the Hub's own, and every process match
  names the Hub's own state directory, which the daemon's `-m harness` command line cannot match. Deploying, rolling
  back and restarting the Hub never stop, restart or reconfigure the daemon.
- **Nothing is published.** No PyPI, no GHCR: the release path is dry-run only ([Release dry run](#release-dry-run)).

## Linux (systemd user unit)

The Hub runs as the systemd **user** unit `agent-harness-hub.service`, as the tower's owner account; no root is needed.
The scripts are bash:

| Script | What |
| --- | --- |
| `deploy-hub.sh` | first install, upgrade to a commit, `--rollback`, `--dry-run`; ends with the check |
| `check-hub.sh` | the read-only check: unit, `/healthz`, `status.json` |
| `install-unit.sh` | writes the unit and `hub.env`, enables the unit (`deploy-hub.sh` runs it on every deployment) |
| `uninstall-unit.sh` | stops the Hub and removes the unit; keeps `<deploy-dir>` |
| `run-hub.sh` | the unit's `ExecStart`: runs the deployed Hub on loopback, logging to `logs/hub.log` |
| `hub-common.sh`, `hub-ops.sh` | shared by the others: paths, stop, start, check, and the `ops/` copies |

### Layout

`<deploy-dir>` (`--deploy-dir`) defaults to `~/.local/share/agent-harness-hub`, the path
[production-signals.md](production-signals.md) reads:

| Path | What |
| --- | --- |
| `venvs/<sha>/` | one virtual environment per deployed commit, built from that commit's `requirements.lock` with hashes |
| `.venv-path`, `.venv-previous` | the active environment and the one it replaced (for rollback) |
| `deployed.json`, `deployed.previous.json` | `commit`, `version`, `deployed_at`, `unmerged` of the active and previous deployment |
| `ops/`, `ops.previous/` | the deployed copy of `ops/hub/*.sh` (the unit runs it) and the copy it replaced; a rollback swaps them with the environments. A deployment that fails is undone and never becomes `ops.previous` (`ops.previous.bak` holds the older copy only while it runs) |
| `hub.env` | the host-specific values, owner-only (`0600`): `HUB_PORT`, `HUB_DAEMON_URL`, `HUB_PUBLIC_URL`, `HUB_STATUS_SECONDS` |
| `state/` | the Hub's `--state-dir`: key, sign-in codes, `status.json` |
| `logs/` | `hub.log` (rotated at 20 MB when the Hub starts), `supervisor.log` (one line per start) |

The unit file is `~/.config/systemd/user/agent-harness-hub.service`. `install-unit.sh` writes it, and nothing in it is
host-specific. `hub.env` holds those values, read through `EnvironmentFile=`. The unit has:

- `Restart=always` and `RestartSec=10`;
- `UMask=0077`, so everything the Hub writes is owner-only;
- `WantedBy=default.target`;
- **no `After=`, `Requires=` or other ordering on the daemon's unit.** A Hub that starts before the daemon only
  reports the daemon as unreachable until it answers, and `Restart=always` covers a Hub that exits. Ordering on the
  daemon is what took the tower's Hub down on 2026-10-29. The daemon's unit has `After=default.target`, so the two
  formed a cycle. systemd broke it by deleting the Hub's start job at boot and recorded no failure. `check-hub.sh`
  reports that case.

### First install

Run from a clean checkout of the commit to deploy (on `main`), as the tower's owner:

```bash
git -C <checkout> pull --ff-only     # the stable checkout, on main
<checkout>/ops/hub/deploy-hub.sh --python <python>
loginctl show-user "$USER" -p Linger  # Linger=yes: the Hub starts at boot without a login
```

`--python` is needed only the first time (any Python 3.12 or later; later deployments reuse the active environment's).
`--dry-run` prints every step without running one. `--port` and `--daemon-url` (defaults `8120` and
`http://127.0.0.1:8100`) and `--public-url` are written to `hub.env`, and later runs keep them. If `Linger=no`, run
`loginctl enable-linger "$USER"` once. Without it, the user manager, and the Hub with it, starts only when the owner
logs in, and the check warns. The script, in order:

1. **Guards.** The checkout is at the requested commit (`--commit`, default HEAD), has no uncommitted changes, and the
   commit is on `origin/main`. `--allow-unmerged` allows a trial of a pull request's head; it prints a warning and
   records `"unmerged": true` in `deployed.json`. One deployment runs at a time (`<deploy-dir>/.deploy.lock`).
2. **Stage, before any downtime.** `git archive` of the commit, a new `venvs/<sha>`, `pip install --require-hashes`
   of the locked requirements, the Hub installed from the archive, and a check that it reports a version and carries
   the UI. A commit without the Linux scripts is refused here. A failure here deletes the new environment and changes
   nothing else.
3. **Swap.** Copy the commit's own `ops/hub/*.sh` from the archive to `ops/` (whichever copy of `deploy-hub.sh` runs,
   the checkout's or the deployed one in `<deploy-dir>/ops`), write the unit and `hub.env` and enable the unit (`install-unit.sh`), stop the Hub (the unit
   and any process whose command line names its state directory), move `.venv-path` to `.venv-previous`, point
   `.venv-path` at the new environment, and write `deployed.json`.
4. **Start and check.** Start the unit. `/healthz` must answer `ok` with the new version and `state/status.json` must
   name the new commit, each within 60 s. Then the full [check](#check-it) must pass. It runs as its own process and
   counts only when it exits 0 with its closing `check: all passed` line, so a check that cannot evaluate fails the
   deployment instead of being skipped.
5. **Roll back on failure.** Once the stop has been attempted (a stop that reports an error may still have stopped
   the Hub, so it counts), any failure, a failed check included, restores the previous pointers,
   records, scripts, unit file and `hub.env`, restarts the previous Hub and says so. If that restart also fails, the error says both.
6. **Clean up.** The environment before the previous one is deleted.

**Moving from a hand-written unit.** If `~/.config/systemd/user/agent-harness-hub.service` exists and
`install-unit.sh` did not write it, the deployment stops before any downtime and changes nothing. First put the old
unit's host-specific values in arguments: `--port`, `--daemon-url`, and `--public-url` for its `--public-url`. Then run
the deployment again with `--replace-unit`. The new unit has no ordering on the daemon. Run
`systemctl --user cat agent-harness-hub` first to see the old unit. Check that its `--state-dir` is
`<deploy-dir>/state`, the directory that holds the Hub key and the claim. A Hub on another state directory starts
unclaimed. If this deployment fails, it puts the old unit file back and restarts its Hub.

### Check it

```bash
<checkout>/ops/hub/check-hub.sh       # or <deploy-dir>/ops/check-hub.sh; exits 1 if any line says FAIL
cat <deploy-dir>/deployed.json        # commit, version, deployed_at, unmerged
cat <deploy-dir>/state/status.json    # heartbeat, claim, last daemon answer
tail -n 50 <deploy-dir>/logs/hub.log
systemctl --user status agent-harness-hub
```

`check-hub.sh` prints one `ok` or `FAIL` line for each of these checks:

- the unit is enabled;
- the unit is active. An enabled unit that is not active fails, with the reason:
  - it **failed**;
  - it was **stopped**;
  - its **start job was dropped**: enabled, inactive, and not started once since the user manager came up. That is
    what an ordering cycle does at boot. Look for `ordering cycle` in `journalctl --user -b`;
- the unit is not ordered on, and does not require, another service;
- `/healthz` on `127.0.0.1:<port>` answers `ok` with the version in `deployed.json`;
- `status.json` was written within five heartbeat intervals (five minutes by default; `HUB_STATUS_MAX_AGE` in seconds
  overrides it) and names the deployed commit.

It warns when lingering is off. Every read-only fetch the sweep uses is in
[production-signals.md](production-signals.md).

### First claim

A fresh Hub is `unclaimed`. On the tower, as the same account:

```bash
"$(cat <deploy-dir>/.venv-path)/bin/python" -m harness_hub signin-link --state-dir <deploy-dir>/state
```

Open the printed link in a browser on the tower, choose **Start claim**, check the six-digit match code, and run the
command the Hub shows on the daemon host (`harness hub approve <id> --match <code>`). The claim signal in
`status.json` then reads `claimed`. This is the owner's step: the daemon accepts it only from the host.

### Upgrade

Deploy a tested commit on `main`, the same way as the first install:

```bash
git -C <checkout> pull --ff-only
<checkout>/ops/hub/deploy-hub.sh                  # HEAD of that checkout
<checkout>/ops/hub/deploy-hub.sh --commit <sha>   # or a named commit (HEAD must equal it)
```

"Tested" means CI is green on the commit (`gate` and `gate-guard`) and the verify skill passes on it
([.claude/skills/verify/SKILL.md](../.claude/skills/verify/SKILL.md)). For a tag, check it out and pass its commit.
A deployment briefly stops the Hub (about 10 s); signed-in browsers keep their cookie for twelve hours, and the claim
survives because the key is in `state/`.

### Roll back

```bash
<checkout>/ops/hub/deploy-hub.sh --rollback
```

Stops the Hub, swaps `.venv-path` with `.venv-previous`, `deployed.json` with `deployed.previous.json` and `ops/` with
`ops.previous/`, starts it and runs the check. Running it again rolls forward to the commit you rolled back from, while
its environment is still on disk. Only one step back is kept; after a second deployment the older environment is
deleted. If a rollback fails, it stops the Hub again, puts everything back and restarts what ran before it.

### Stop and uninstall

```bash
systemctl --user stop agent-harness-hub           # stops the Hub until the next start or boot
<checkout>/ops/hub/uninstall-unit.sh              # stops it and removes the unit: no start at boot
```

A stopped unit is still enabled, so `check-hub.sh` reports it until it runs again (`systemctl --user start
agent-harness-hub`). `uninstall-unit.sh` keeps `<deploy-dir>`, including `state/` and `hub.env`; delete
`<deploy-dir>` to remove everything. The daemon's own Hub record remains until you run `harness hub release --confirm`
on the daemon host.

### Tailnet access

Loopback is enough on the tower itself. To open the Hub from another device on your tailnet, publish it with
`tailscale serve`'s HTTPS proxy on its own port (`443` and the other ports in use are the daemon's and other services')
and give the Hub that address, so its cookies are `Secure` and the Host check allows it:

```bash
tailscale serve --bg --https=8120 http://127.0.0.1:8120
<deploy-dir>/ops/install-unit.sh --public-url https://<tailnet-hostname>:8120   # written to hub.env only
systemctl --user restart agent-harness-hub        # the Hub reads --public-url at start
```

`tailscale serve status` should then list `:8120 (tailnet only)` with `proxy http://127.0.0.1:8120`. This changes the
node's serve config, which other services share, so it is the owner's step. Later deployments keep the address, since
it is in `hub.env`; `--public-url ''` clears it.

**Sign-in on Linux uses sign-in links only.** The README's "With your tailnet identity" describes signing in with the
tailnet identity that `tailscale serve` passes on. The Hub trusts that identity only after a peer check that
tailscaled sent it, and that peer check is implemented for Windows only. On Linux the Hub removes the identity headers,
so open the Hub with a sign-in link (`signin-link`, as in [First claim](#first-claim)). The
`--trust-unverified-identity-headers` option trusts any local process and is not used here.

## Windows (logon task)

The same deployment as a per-user logon task, `AgentHarness-Hub`, from the PowerShell scripts in `ops/hub/`
(`deploy-hub.ps1`, `run-hub.ps1`, `install-task.ps1`, `hub-common.ps1`, `hub-ops.ps1`). Here `<deploy-dir>` is
`-DeployDir`, whose default is in the `param` block of `ops/hub/deploy-hub.ps1`, and `<python>` is a `python.exe`. The
layout and records are the same as on Linux, with `ops\` holding the `.ps1` copies and no `hub.env`: the task's
arguments carry those values.

### Layout

`<deploy-dir>` (`-DeployDir`):

| Path | What |
| --- | --- |
| `venvs\<sha>\` | one virtual environment per deployed commit, built from that commit's `requirements.lock` with hashes |
| `.venv-path`, `.venv-previous` | the active environment and the one it replaced (for rollback) |
| `deployed.json`, `deployed.previous.json` | `commit`, `version`, `deployed_at`, `unmerged` of the active and previous deployment |
| `ops\`, `ops.previous\` | the deployed copy of `ops/hub/*.ps1` (the logon task runs it) and the copy it replaced; a rollback swaps them with the environments. A deployment that fails is undone and never becomes `ops.previous` (`ops.previous.bak` holds the older copy only while it runs) |
| `state\` | the Hub's `--state-dir`: key, sign-in codes, `status.json` |
| `logs\` | `hub.log`, `supervisor.log` |

### First install

Run from a clean checkout of the commit to deploy (on `main`), in PowerShell as the tower's signed-in owner. No
elevation is needed: the logon task is per-user, like the daemon's.

```powershell
git -C "<checkout>" pull --ff-only  # the stable checkout, on main
& "<checkout>\ops\hub\deploy-hub.ps1" `
    -Python "<python>"
```

`-Python` is needed only the first time (any Python 3.12 or later; later deployments reuse the active environment's).
`-DryRun` prints every step without running one. The script, in order:

1. **Guards.** The checkout is at the requested commit (`-Commit`, default HEAD), has no uncommitted changes, and the
   commit is on `origin/main`. `-AllowUnmerged` allows a trial of a pull request's head; it prints a warning and
   records `"unmerged": true` in `deployed.json`.
2. **Stage, before any downtime.** `git archive` of the commit, a new `venvs\<sha>`, `pip install --require-hashes`
   of the locked requirements, the Hub installed from the archive, and a check that it reports a version and carries
   the UI. A failure here deletes the new environment and changes nothing else.
3. **Swap.** Stop the Hub (the `AgentHarness-Hub` task and any process whose command line names its state
   directory), move `.venv-path` to `.venv-previous`, point `.venv-path` at the new environment, write
   `deployed.json`, copy `ops\`.
4. **Start and check.** Start the task; `/healthz` must answer `ok` with the new version, and `state\status.json`
   must name the new commit, each within 60 s.
5. **Roll back on failure.** After the stop, any failure restores the previous pointers, stamp and scripts, restarts the
   previous Hub and says so. If that restart also fails, the error says both.
6. **Clean up.** The environment before the previous one is deleted.

The first run registers the task if it does not exist (`install-task.ps1`): `AgentHarness-Hub`, at logon of the
signed-in user, hidden, restarted by `run-hub.ps1` ten seconds after any exit. The tower auto-logs-in on boot, so the
Hub comes back after every restart, next to the daemon.

### Check it

```powershell
Invoke-RestMethod http://127.0.0.1:8120/healthz                  # ok true, version
Get-Content "<deploy-dir>\deployed.json"                         # commit, version, deployed_at, unmerged
Get-Content "<deploy-dir>\state\status.json"                     # heartbeat, claim, last daemon answer
Get-Content "<deploy-dir>\logs\hub.log" -Tail 50
```

Every read-only fetch the sweep uses is in [production-signals.md](production-signals.md).

### First claim

A fresh Hub is `unclaimed`. On the tower, as the same account:

```powershell
& "<deploy-dir>\venvs\<sha>\Scripts\python.exe" -m harness_hub signin-link --state-dir "<deploy-dir>\state"
```

Open the printed link in a browser on the tower, choose **Start claim**, check the six-digit match code, and run the
command the Hub shows on the daemon host (`harness hub approve <id> --match <code>`). The claim signal in
`status.json` then reads `claimed`. This is the owner's step: the daemon accepts it only from the host.

### Upgrade

Deploy a tested commit on `main`, the same way as the first install:

```powershell
git -C "<checkout>" pull --ff-only
& "<checkout>\ops\hub\deploy-hub.ps1"                # HEAD of that checkout
& "<checkout>\ops\hub\deploy-hub.ps1" -Commit <sha>  # or a named commit (HEAD must equal it)
```

"Tested" means CI is green on the commit (`gate` and `gate-guard`) and the verify skill passes on it
([.claude/skills/verify/SKILL.md](../.claude/skills/verify/SKILL.md)). For a tag, check it out and pass its commit.
A deployment briefly stops the Hub (about 10 s); signed-in browsers keep their cookie for twelve hours, and the claim
survives because the key is in `state\`.

### Roll back

```powershell
& "<checkout>\ops\hub\deploy-hub.ps1" -Rollback
```

Stops the Hub, swaps `.venv-path` with `.venv-previous`, `deployed.json` with `deployed.previous.json` and `ops\` with
`ops.previous\`, starts it and checks `/healthz`. Running it again rolls forward to the commit you rolled back from, while its environment is
still on disk. Only one step back is kept; after a second deployment the older environment is deleted. If a rollback
fails, it stops the Hub again, puts everything back and restarts what ran before it.

### Stop and uninstall

```powershell
Stop-ScheduledTask AgentHarness-Hub                              # the supervisor and the Hub stop
Unregister-ScheduledTask AgentHarness-Hub -Confirm:$false        # no start at logon
```

`state\` is kept; delete `<deploy-dir>` to remove everything. The daemon's own Hub record remains until
you run `harness hub release --confirm` on the daemon host.

### Tailnet access

Loopback is enough on the tower itself. To open the Hub from another device on your tailnet, publish it with
`tailscale serve`'s HTTPS proxy on its own port (`443` and the other ports in use are the daemon's and other services')
and give the Hub that address, so its cookies are `Secure` and the Host check allows it:

```powershell
tailscale serve --bg --https=8120 http://127.0.0.1:8120
& "<checkout>\ops\hub\install-task.ps1" -PublicUrl https://<tailnet-hostname>:8120
Stop-ScheduledTask AgentHarness-Hub; Start-ScheduledTask AgentHarness-Hub   # the Hub reads --public-url at start
```

`tailscale serve status` should then list `:8120 (tailnet only)` with `proxy http://127.0.0.1:8120`. This changes the
node's serve config, which other services share, so it is the owner's step; see the README's "With your tailnet
identity" for how the Hub trusts tailnet sign-in, and why a raw TCP forward is never used. Use `-PublicUrl` on the
next `deploy-hub.ps1` runs too (it only matters when the task is first registered; `install-task.ps1` changes it
later).

## Release dry run

Publishing to PyPI and GHCR is out of scope until the owner decides it (README, "Release"). What can be rehearsed now,
with no upload, is the release's build:

- **In CI**, `release-dry-run.yml` (on `vX.Y.Z` and pre-release `vX.Y.Z-*` tags, and by manual trigger) runs the
  release's `build` steps without `pypi` or `ghcr`. The owner adds it: the workers' GitHub App cannot push under
  `.github/workflows/`, so its exact text is in the comment on issue #146, not in this change. It will: build the wheel and sdist, install the wheel into a clean
  environment from `requirements.lock` and check its version and UI, build the image and run
  `scripts/smoke-image.sh`. It has only `contents: read`, no environment and no secrets, and uploads nothing.
- **Locally**, the same steps are the `package` row of AGENTS.md ("How to prove a change works").
