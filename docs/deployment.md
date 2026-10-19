# Deploying the Hub on the tower

The Hub runs on the tower next to the agent-harness daemon, as a logon task, so real use produces real signals
([production-signals.md](production-signals.md)). The scripts are in `ops/hub/` and are modelled on agent-harness
`ops/harness/` (`deploy-ci.ps1`, `run-daemon.ps1`, `install-task.ps1`).

**Rules**

- **Loopback or tailnet only.** The Hub listens on `127.0.0.1:8120` and nothing else (`hub-common.ps1` pins the host;
  there is no switch for another). A browser on the tailnet reaches it through `tailscale serve`'s HTTPS proxy (step
  5). Never `--host 0.0.0.0`, never a LAN address, never `tailscale funnel`.
- **No secrets in the repo.** Everything secret stays in `D:\Agents\agent-harness-hub\state` (owner-only ACL): the Hub
  key and sign-in codes. The scripts, `deployed.json` and `status.json` hold none.
- **The daemon is never touched.** Every process match in `hub-common.ps1` names the Hub's own state directory, and the
  daemon's `-m harness` command line cannot match it. Deploying, rolling back and restarting the Hub never stop,
  restart or reconfigure `AgentHarness-Daemon`.
- **Nothing is published.** No PyPI, no GHCR: the release path is dry-run only ([Release dry run](#release-dry-run)).

## Layout

`D:\Agents\agent-harness-hub` (`-DeployDir`):

| Path | What |
| --- | --- |
| `venvs\<sha>\` | one virtual environment per deployed commit, built from that commit's `requirements.lock` with hashes |
| `.venv-path`, `.venv-previous` | the active environment and the one it replaced (for rollback) |
| `deployed.json`, `deployed.previous.json` | `commit`, `version`, `deployed_at`, `unmerged` of the active and previous deployment |
| `ops\`, `ops.previous\` | the deployed copy of `ops/hub/*.ps1` (the logon task runs it) and the copy it replaced; a rollback swaps them with the environments |
| `state\` | the Hub's `--state-dir`: key, sign-in codes, `status.json` |
| `logs\` | `hub.log`, `supervisor.log` |

## First install

Run from a clean checkout of the commit to deploy (on `main`), in PowerShell as the tower's signed-in owner. No
elevation is needed: the logon task is per-user, like the daemon's.

```powershell
git -C D:\Projects\agent-harness-hub pull --ff-only          # the stable checkout, on main
D:\Projects\agent-harness-hub\ops\hub\deploy-hub.ps1 `
    -Python C:\Users\dflip\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe
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

## Check it

```powershell
Invoke-RestMethod http://127.0.0.1:8120/healthz                  # ok true, version
Get-Content D:\Agents\agent-harness-hub\deployed.json            # commit, version, deployed_at, unmerged
Get-Content D:\Agents\agent-harness-hub\state\status.json        # heartbeat, claim, last daemon answer
Get-Content D:\Agents\agent-harness-hub\logs\hub.log -Tail 50
```

Every read-only fetch the sweep uses is in [production-signals.md](production-signals.md).

## First claim

A fresh Hub is `unclaimed`. On the tower, as the same account:

```powershell
D:\Agents\agent-harness-hub\venvs\<sha>\Scripts\python.exe -m harness_hub signin-link --state-dir D:\Agents\agent-harness-hub\state
```

Open the printed link in a browser on the tower, choose **Start claim**, check the six-digit match code, and run the
command the Hub shows on the daemon host (`harness hub approve <id> --match <code>`). The claim signal in
`status.json` then reads `claimed`. This is the owner's step: the daemon accepts it only from the host.

## Upgrade

Deploy a tested commit on `main`, the same way as the first install:

```powershell
git -C D:\Projects\agent-harness-hub pull --ff-only
D:\Projects\agent-harness-hub\ops\hub\deploy-hub.ps1                 # HEAD of that checkout
D:\Projects\agent-harness-hub\ops\hub\deploy-hub.ps1 -Commit <sha>   # or a named commit (HEAD must equal it)
```

"Tested" means CI is green on the commit (`gate` and `gate-guard`) and the verify skill passes on it
([.claude/skills/verify/SKILL.md](../.claude/skills/verify/SKILL.md)). For a tag, check it out and pass its commit.
A deployment briefly stops the Hub (about 10 s); signed-in browsers keep their cookie for twelve hours, and the claim
survives because the key is in `state\`.

## Roll back

```powershell
D:\Projects\agent-harness-hub\ops\hub\deploy-hub.ps1 -Rollback
```

Stops the Hub, swaps `.venv-path` with `.venv-previous`, `deployed.json` with `deployed.previous.json` and `ops\` with
`ops.previous\`, starts it and checks `/healthz`. Running it again rolls forward to the commit you rolled back from, while its environment is
still on disk. Only one step back is kept; after a second deployment the older environment is deleted. If a rollback
fails, it stops the Hub again, puts everything back and restarts what ran before it.

## Stop and uninstall

```powershell
Stop-ScheduledTask AgentHarness-Hub                              # the supervisor and the Hub stop
Unregister-ScheduledTask AgentHarness-Hub -Confirm:$false        # no start at logon
```

`state\` is kept; delete `D:\Agents\agent-harness-hub` to remove everything. The daemon's own Hub record remains until
you run `harness hub release --confirm` on the daemon host.

## Tailnet access

Loopback is enough on the tower itself. To open the Hub from another device on your tailnet, publish it with
`tailscale serve`'s HTTPS proxy on its own port (`443` and the other ports in use are the daemon's and other services')
and give the Hub that address, so its cookies are `Secure` and the Host check allows it:

```powershell
tailscale serve --bg --https=8120 http://127.0.0.1:8120
D:\Projects\agent-harness-hub\ops\hub\install-task.ps1 -PublicUrl https://dflippotower.tail7c9f1e.ts.net:8120
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
