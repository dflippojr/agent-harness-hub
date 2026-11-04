#!/usr/bin/env bash
# ExecStart of the agent-harness-hub.service user unit (install-unit.sh), run from the deployment's own copy in
# <deploy-dir>/ops. systemd restarts it when it exits (Restart=always), so there is no supervisor loop as in run-hub.ps1.
# The Python comes from the deployment's .venv-path pointer (deploy-hub.sh); HUB_BUILD_COMMIT comes from deployed.json,
# so the Hub's status.json (docs/production-signals.md) names the commit it runs. The port, daemon URL and public URL
# come from hub.env through the unit's EnvironmentFile=. The Hub listens on loopback only.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=hub-common.sh
source "$here/hub-common.sh"

hub_set_paths "${HUB_DEPLOY_DIR:-$(dirname -- "$here")}"
PORT=${HUB_PORT:-} DAEMON_URL=${HUB_DAEMON_URL:-} PUBLIC_URL=${HUB_PUBLIC_URL:-} STATUS_SECONDS=${HUB_STATUS_SECONDS:-}
hub_load_env

[[ -f $POINTER ]] || hub_die "no deployment yet: $POINTER is missing; run deploy-hub.sh"
venv=$(hub_read_pointer "$POINTER")
[[ $venv == /* ]] || hub_die "invalid deployment virtual environment pointer: $POINTER"
python=$venv/bin/python
[[ -x $python ]] || hub_die "Hub Python is missing: $python"
commit=$(hub_json_field "$STAMP" commit)
[[ $commit =~ ^[0-9a-f]{40}$ ]] || commit=''

mkdir -p -- "$LOGS" "$STATE"
log=$LOGS/hub.log
log_size=0
[[ -f $log ]] && log_size=$(stat -c %s -- "$log")
if ((log_size > 20 * 1024 * 1024)); then mv -f -- "$log" "$log.prev"; fi
printf '%s starting Hub %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$commit" >>"$LOGS/supervisor.log"

args=(--daemon-url "$DAEMON_URL" --state-dir "$STATE" --host "$HUB_HOST" --port "$PORT" --status-interval "$STATUS_SECONDS")
[[ -n $PUBLIC_URL ]] && args+=(--public-url "$PUBLIC_URL")
cd -- "$DEPLOY"
HUB_BUILD_COMMIT=$commit exec "$python" -u -m harness_hub "${args[@]}" >>"$log" 2>&1
