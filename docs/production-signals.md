# Production signals

How the deployed Hub shows whether it is working, for the tower's production-signal sweep
([agent-ops docs/production-signals.md](https://github.com/dflippojr/agent-ops/blob/main/docs/production-signals.md),
issue agent-ops#52) and for the owner. Every fetch here is read-only: nothing below changes the Hub, its daemon or any
file. How the Hub is deployed is in [deployment.md](deployment.md).

## Where it runs

The Hub runs on the tower, next to the agent-harness daemon, as the `AgentHarness-Hub` logon task. It listens on
`127.0.0.1:8120` only. Its files are under `D:\Agents\agent-harness-hub`:

| Path | What |
| --- | --- |
| `deployed.json` | `commit`, `version`, `deployed_at`, `unmerged`: written by `ops/hub/deploy-hub.ps1` |
| `state\status.json` | the Hub's heartbeat, rewritten every 60 s (below). Owner-only; nothing serves it over HTTP |
| `logs\hub.log` | the Hub's log (Python logging format, plus uvicorn's own lines), rotated at 20 MB |
| `logs\supervisor.log` | the supervisor's start and exit lines |

`status.json` holds typed values only, never a credential:

```json
{"version": "0.1.0", "build": "0123456789abcdef0123456789abcdef01234567", "started_at": "2026-10-19T20:00:00Z",
 "written_at": "2026-10-19T20:05:00Z", "claim": "claimed", "daemon_ok": true,
 "daemon_last_ok_at": "2026-10-19T20:05:00Z"}
```

- `build` is the commit in `deployed.json` when the Hub started, or `unknown`.
- `claim` is the Hub's claim state: `unclaimed`, `pending`, `claimed`, or an outcome such as `refused` or `lost`.
- `daemon_last_ok_at` is the last time the daemon's public `/health` answered `ok` to the Hub; `null` if it never has
  since the Hub started. `daemon_ok` is the latest poll's result.

## Signals

| Signal | Fetch (read-only) | Expected freshness |
| --- | --- | --- |
| Hub answers | `curl -fsS http://127.0.0.1:8120/healthz` | `{"ok":true,"version":"<version>"}` within 5 s at any time |
| Deployed SHA | `python -c "import json; print(json.load(open(r'D:\Agents\agent-harness-hub\deployed.json'))['commit'])"` | the commit last deployed from `main` |
| Heartbeat | `.written_at` in `state\status.json` | under 5 min old: the Hub's event loop is alive |
| Claim state | `.claim` in `state\status.json` | `claimed` once the owner has approved the Hub |
| Last successful daemon poll | `.daemon_last_ok_at` in `state\status.json` | under 10 min old while the daemon is up |
| Recent errors | `tail -c 4194304 D:/Agents/agent-harness-hub/logs/hub.log \| grep -E -A30 ' (ERROR\|CRITICAL) '` | none in 24 h |

The age of the last daemon poll is `now - daemon_last_ok_at`. A stale `daemon_last_ok_at` with a fresh `written_at`
means the Hub is up and the daemon is not answering; a stale `written_at` means the Hub itself has stopped.

PowerShell, as one sweep reading from the tower:

```powershell
$root = 'D:\Agents\agent-harness-hub'
Invoke-RestMethod http://127.0.0.1:8120/healthz
Get-Content -Raw "$root\deployed.json" | ConvertFrom-Json
$status = Get-Content -Raw "$root\state\status.json" | ConvertFrom-Json
$status
# [datetimeoffset] keeps the "Z"; a plain [datetime] cast turns it into local time and the age is off by the UTC offset
$now = (Get-Date).ToUniversalTime()
"heartbeat age:   $([int]($now - ([datetimeoffset]$status.written_at).UtcDateTime).TotalSeconds) s"
"daemon poll age: $([int]($now - ([datetimeoffset]$status.daemon_last_ok_at).UtcDateTime).TotalSeconds) s"
Get-Content "$root\logs\hub.log" -Tail 2000 | Select-String ' (ERROR|CRITICAL) '
```

## The block the sweep reads

```yaml production-signals
signals:
  - id: health
    kind: health
    description: The deployed Hub answers /healthz with ok true.
    fetch: {type: http, url: "http://127.0.0.1:8120/healthz", field: ok}
    expect: {equals: true}
  - id: deployed-sha
    kind: deployed_sha
    description: Commit the deployed Hub runs (deployed.json, written by ops/hub/deploy-hub.ps1).
    fetch: {type: file, path: D:/Agents/agent-harness-hub/deployed.json, field: commit}
  - id: heartbeat
    kind: freshness
    description: The Hub's status.json was rewritten recently (it is written every 60 s).
    max_age: 5m
    hint: The Hub process or its supervisor stopped; see logs/supervisor.log and docs/deployment.md.
    fetch: {type: file, path: D:/Agents/agent-harness-hub/state/status.json, field: written_at}
  - id: daemon-poll
    kind: freshness
    description: The last time the Hub's poll of the daemon's /health succeeded.
    max_age: 10m
    hint: The Hub is up but the daemon is not answering it; check the daemon at 127.0.0.1:8100.
    fetch: {type: file, path: D:/Agents/agent-harness-hub/state/status.json, field: daemon_last_ok_at}
  - id: claim
    kind: health
    description: The Hub has claimed its daemon.
    fetch: {type: file, path: D:/Agents/agent-harness-hub/state/status.json, field: claim}
    expect: {equals: claimed}
    public: {values: [unclaimed, pending, claimed, refused, expired, denied, released, rate_limited, failed, lost, persisting]}
  - id: hub-errors
    kind: errors
    description: ERROR and CRITICAL records in the deployed Hub's log.
    fetch: {type: log, path: D:/Agents/agent-harness-hub/logs/hub.log, window: 24h}
    public: {loggers: [harness_hub.status, harness_hub.server, harness_hub.claim, uvicorn.error]}
```

`deployed.json` and `status.json` hold no secrets, so the sweep may read them. The Hub's state directory also holds
the Hub key (`hub-state.json`) and sign-in codes; no signal reads those.

## When to file an issue

File a `bug` + `production` issue with the deployed commit, the fetched values and the time (never a token or sign-in
code) when:

- **Unexpected errors:** `hub-errors` finds an ERROR or CRITICAL record: a Hub bug, or a daemon change it did not
  expect. These are the real-environment defects (proxies, tailnet host names, https sign-in) that only use finds.
- **Stale deploy:** `deployed-sha` is behind `main` by commits merged more than a day ago, or `unmerged` is `true`
  in `deployed.json` (a trial deployment was left running).
- **Unclaimed:** `claim` is anything but `claimed` more than a day after a deployment. The owner approves the Hub's
  claim on the tower (`docs/deployment.md`, "First claim").

Raise a silence alarm instead when a signal is older than its bound:

- `/healthz` does not answer, or `heartbeat` is older than 5 minutes.
- `daemon-poll` is older than 10 minutes with the daemon itself up.

These are expected and need no issue: a `daemon-poll` gap while the daemon restarts (a settings restart takes under a
minute), and `claim` of `pending` for the ten minutes a claim stays open.

## Not covered

- Alerting and the sweep itself live in dflippojr/agent-ops#52.
- The Hub reads its daemon only through the daemon's public `/health`; per-App usage and the daemon's own errors are
  the daemon's signals (agent-harness `docs/production-signals`).
