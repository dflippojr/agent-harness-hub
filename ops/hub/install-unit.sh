#!/usr/bin/env bash
# Installs the Hub's systemd user unit (~/.config/systemd/user/agent-harness-hub.service) and its host-specific
# environment file (<deploy-dir>/hub.env), and enables the unit. The Linux counterpart of install-task.ps1. No root is
# needed. deploy-hub.sh runs it on every deployment; run it yourself to change the port, daemon URL or public URL.
#
# The unit has no After=, Requires= or other ordering on the daemon's unit: Restart=always covers a daemon that is not up
# yet, and an ordering on it formed a cycle that made systemd drop the Hub's start job at boot (issue #210).
#
# Usage: install-unit.sh [--deploy-dir DIR] [--port N] [--daemon-url URL] [--public-url https://...]
#                        [--status-seconds N] [--replace-unit]
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=hub-common.sh
source "$here/hub-common.sh"

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
deploy_dir='' replace=0 PORT='' DAEMON_URL='' PUBLIC_URL='' STATUS_SECONDS='' given=()
while (($#)); do
    case $1 in
        --deploy-dir) deploy_dir=${2:?--deploy-dir needs a value}; shift 2 ;;
        --port) PORT=${2:?--port needs a value}; given+=(HUB_PORT); shift 2 ;;
        --daemon-url) DAEMON_URL=${2:?--daemon-url needs a value}; given+=(HUB_DAEMON_URL); shift 2 ;;
        --public-url) PUBLIC_URL=${2?--public-url needs a value}; given+=(HUB_PUBLIC_URL); shift 2 ;;
        --status-seconds) STATUS_SECONDS=${2:?--status-seconds needs a value}; given+=(HUB_STATUS_SECONDS); shift 2 ;;
        --replace-unit) replace=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) hub_die "unknown argument: $1 (see --help)" ;;
    esac
done
hub_set_paths "${deploy_dir:-$(hub_own_deploy_dir "$here")}"
public_given=0
for key in "${given[@]}"; do [[ $key == HUB_PUBLIC_URL ]] && public_given=1; done
explicit_public=$PUBLIC_URL
hub_load_env
# An explicit empty --public-url clears the address (back to loopback only).
((public_given)) && PUBLIC_URL=$explicit_public

[[ -f $DEPLOY/ops/run-hub.sh ]] ||
    hub_die "run-hub.sh is not deployed yet: $DEPLOY/ops/run-hub.sh (run deploy-hub.sh)"

# A unit this script did not write (a hand-written one) may hold values that belong in hub.env; it is replaced only when
# asked, so they are not lost.
marker='# Written by ops/hub/install-unit.sh'
if [[ -f $UNIT_FILE ]] && ! grep -qxF "$marker" "$UNIT_FILE" && ((!replace)); then
    hub_die "$UNIT_FILE was not written by install-unit.sh. Move any host-specific values (port, daemon URL, \
--public-url) into $ENV_FILE or pass them as arguments, then rerun with --replace-unit (docs/deployment.md)."
fi

umask 077
mkdir -p -- "$DEPLOY" "$(dirname -- "$UNIT_FILE")"
env_text="# Host-specific values for $HUB_UNIT (EnvironmentFile=). Not in the repository, which is public.
# install-unit.sh rewrites the HUB_ lines; restart the unit after changing them.
HUB_PORT=$PORT
HUB_DAEMON_URL=$DAEMON_URL
HUB_PUBLIC_URL=$PUBLIC_URL
HUB_STATUS_SECONDS=$STATUS_SECONDS"
if [[ -f $ENV_FILE ]]; then
    # Keep any other lines the owner added.
    extra=$(grep -vE '^[[:space:]]*(#|$|HUB_(PORT|DAEMON_URL|PUBLIC_URL|STATUS_SECONDS)=)' "$ENV_FILE" || true)
    [[ -n $extra ]] && env_text+=$'\n'"$extra"
fi
hub_write_file "$ENV_FILE" "$env_text"
chmod 0600 "$ENV_FILE"

unit_text="$marker
# (docs/deployment.md). Rerun it rather than editing this file; host-specific values are in hub.env.
[Unit]
Description=Agent Harness Hub (loopback only, $HUB_HOST:$PORT)
# No ordering on the daemon's unit: Restart=always covers a daemon that is not up yet, and an ordering on it can form a
# cycle that makes systemd drop this unit's start job at boot without recording a failure.

[Service]
Type=simple
Environment=HUB_DEPLOY_DIR=$DEPLOY
EnvironmentFile=$ENV_FILE
WorkingDirectory=$DEPLOY
ExecStart=/usr/bin/env bash $DEPLOY/ops/run-hub.sh
Restart=always
RestartSec=10
UMask=0077

[Install]
WantedBy=default.target"
changed=1
if [[ -f $UNIT_FILE && "$(cat -- "$UNIT_FILE")" == "$unit_text" ]]; then changed=0; fi
if ((changed)); then
    hub_write_file "$UNIT_FILE" "$unit_text"
    chmod 0644 "$UNIT_FILE"
    systemctl --user daemon-reload
fi
systemctl --user enable "$HUB_UNIT" >/dev/null 2>&1 || systemctl --user enable "$HUB_UNIT"
hub_say "$HUB_UNIT installed and enabled ($UNIT_FILE; settings in $ENV_FILE)."
if [[ $(systemctl --user is-active "$HUB_UNIT" 2>/dev/null || true) == active ]]; then
    hub_say "The running Hub reads its settings at start: systemctl --user restart $HUB_UNIT"
fi
