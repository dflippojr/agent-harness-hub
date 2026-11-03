# shellcheck shell=bash
# Shared by deploy-hub.sh, check-hub.sh, install-unit.sh, uninstall-unit.sh and run-hub.sh (source it). The Linux
# counterpart of hub-common.ps1: where the deployed Hub lives, its systemd user unit, and how to stop, start, health-check
# and check it. It never touches the agent-harness daemon: the only unit it names is the Hub's own, and every process
# match below names this deployment's state directory, which the daemon's `-m harness` command line cannot match.
#
# Host-specific values (port, daemon URL, the `tailscale serve` address) live in <deploy-dir>/hub.env, never in this
# repository, which is public.

HUB_UNIT=agent-harness-hub.service
# Loopback only. A tailnet address is published with `tailscale serve` (docs/deployment.md), never by listening on it.
HUB_HOST=127.0.0.1
HUB_DEFAULT_PORT=8120
HUB_DEFAULT_DAEMON_URL=http://127.0.0.1:8100
HUB_DEFAULT_STATUS_SECONDS=60
# How long a start may take to answer /healthz, and how often it is polled. Tests shorten them.
HUB_START_TIMEOUT=${HUB_START_TIMEOUT:-60}
HUB_POLL_SECONDS=${HUB_POLL_SECONDS:-2}

hub_say() { printf '%s\n' "$*"; }
hub_warn() { printf 'warning: %s\n' "$*" >&2; }
hub_die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

hub_default_deploy_dir() { printf '%s\n' "${HOME:?HOME is not set}/.local/share/agent-harness-hub"; }
hub_unit_dir() { printf '%s\n' "${XDG_CONFIG_HOME:-${HOME:?HOME is not set}/.config}/systemd/user"; }

# Sets the deployment's paths from its directory. The names match Get-HubPaths in hub-common.ps1.
hub_set_paths() {
    local deploy=$1
    [[ $deploy == /* ]] || hub_die "the deploy directory must be an absolute path: $deploy"
    # The unit names these paths unquoted (EnvironmentFile=, ExecStart=), and systemd expands % and $ in them.
    local unsafe='[[:space:]%$\\"'"'"']'
    [[ $deploy =~ $unsafe ]] && hub_die "the deploy directory may not contain spaces, quotes, %, \$ or \\: $deploy"
    DEPLOY=${deploy%/}
    STATE=$DEPLOY/state
    LOGS=$DEPLOY/logs
    VENVS=$DEPLOY/venvs
    POINTER=$DEPLOY/.venv-path
    PREVIOUS=$DEPLOY/.venv-previous
    STAMP=$DEPLOY/deployed.json
    STAMP_OLD=$DEPLOY/deployed.previous.json
    ENV_FILE=$DEPLOY/hub.env
    UNIT_FILE=$(hub_unit_dir)/$HUB_UNIT
    export DEPLOY STATE LOGS VENVS POINTER PREVIOUS STAMP STAMP_OLD ENV_FILE UNIT_FILE
}

# The value of KEY in hub.env (the last assignment wins), without surrounding quotes; empty when unset. The file is
# read as data, never sourced, the way systemd's EnvironmentFile= reads it.
hub_env_value() {
    local key=$1 line value='' found
    [[ -f $ENV_FILE ]] || return 0
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        [[ ${BASH_REMATCH[1]} == "$key" ]] || continue
        found=${BASH_REMATCH[2]}
        if [[ $found =~ ^\"(.*)\"$ || $found =~ ^\'(.*)\'$ ]]; then found=${BASH_REMATCH[1]}; fi
        value=$found
    done <"$ENV_FILE"
    printf '%s\n' "$value"
}

# PORT, DAEMON_URL, PUBLIC_URL and STATUS_SECONDS: a value the caller already set (a command-line argument) wins, then
# hub.env, then the default.
hub_load_env() {
    PORT=${PORT:-$(hub_env_value HUB_PORT)}
    PORT=${PORT:-$HUB_DEFAULT_PORT}
    DAEMON_URL=${DAEMON_URL:-$(hub_env_value HUB_DAEMON_URL)}
    DAEMON_URL=${DAEMON_URL:-$HUB_DEFAULT_DAEMON_URL}
    PUBLIC_URL=${PUBLIC_URL:-$(hub_env_value HUB_PUBLIC_URL)}
    STATUS_SECONDS=${STATUS_SECONDS:-$(hub_env_value HUB_STATUS_SECONDS)}
    STATUS_SECONDS=${STATUS_SECONDS:-$HUB_DEFAULT_STATUS_SECONDS}
    if ! [[ $PORT =~ ^[0-9]{1,5}$ ]] || ((10#$PORT < 1 || 10#$PORT > 65535)); then hub_die "invalid port: $PORT"; fi
    [[ $STATUS_SECONDS =~ ^[0-9]+$ ]] || hub_die "invalid status interval: $STATUS_SECONDS"
    [[ $DAEMON_URL =~ ^https?://[^[:space:]]+$ ]] || hub_die "invalid daemon URL: $DAEMON_URL"
    [[ -z $PUBLIC_URL || $PUBLIC_URL =~ ^https://[^[:space:]]+$ ]] || hub_die "the public URL must be https: $PUBLIC_URL"
}

# One field of a JSON object file, printed as a string (true/false for booleans, empty when missing or unreadable).
hub_json_field() {
    local file=$1 key=$2
    [[ -f $file ]] || return 0
    python3 -I -c '
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2])
except Exception:
    sys.exit(0)
if isinstance(value, bool):
    print("true" if value else "false")
elif value is not None:
    print(value)
' "$file" "$key"
}

hub_read_pointer() {
    local value=''
    [[ -f $1 ]] && value=$(<"$1")
    value=${value//$'\r'/}
    printf '%s\n' "${value%%$'\n'*}"
}

# Writes a file through a temporary copy and a rename, so a reader never sees half of it.
hub_write_file() {
    local path=$1 value=$2
    printf '%s\n' "$value" >"$path.tmp" && mv -f -- "$path.tmp" "$path"
}

# Process ids of a Hub on this deployment's state directory that systemd does not hold (one started by hand). A
# disposable Hub from the verify skill or a test has another state directory, so it never matches.
hub_processes() {
    local pid args
    while read -r pid args; do
        [[ $pid == "$$" ]] && continue
        [[ $args == *"-m harness_hub"* && " $args " == *" --state-dir $STATE "* ]] && printf '%s\n' "$pid"
    done < <(ps -eo pid=,args=)
    return 0
}

hub_unit_installed() { [[ -f $UNIT_FILE ]]; }

hub_stop() {
    local attempt remaining
    if hub_unit_installed; then
        systemctl --user stop "$HUB_UNIT" || { hub_warn "could not stop $HUB_UNIT"; return 1; }
    fi
    for ((attempt = 0; attempt < 30; attempt++)); do
        remaining=$(hub_processes)
        [[ -z $remaining ]] && break
        # shellcheck disable=SC2086 # one pid per word
        kill $remaining 2>/dev/null || true
        sleep 1
    done
    [[ -z $(hub_processes) ]] || { hub_warn 'could not stop all Hub processes'; return 1; }
    hub_say "$HUB_UNIT stopped."
}

# True when /healthz answers ok (and names VERSION, when given).
hub_health() {
    local version=${1:-} body
    body=$(curl -fsS --max-time 3 "http://$HUB_HOST:$PORT/healthz" 2>/dev/null) || return 1
    python3 -I -c '
import json, sys
try:
    body = json.loads(sys.argv[1])
except ValueError:
    sys.exit(1)
sys.exit(0 if body.get("ok") is True and (not sys.argv[2] or body.get("version") == sys.argv[2]) else 1)
' "$body" "$version"
}

hub_wait() {  # seconds to wait for a condition command, polled every HUB_POLL_SECONDS
    local deadline=$(($(date +%s) + $1))
    shift
    while :; do
        "$@" && return 0
        (($(date +%s) >= deadline)) && return 1
        sleep "$HUB_POLL_SECONDS"
    done
}

hub_start() {
    local version=${1:-}
    systemctl --user start "$HUB_UNIT" || { hub_warn "could not start $HUB_UNIT"; return 1; }
    if hub_wait "$HUB_START_TIMEOUT" hub_health "$version"; then
        hub_say "Hub answers /healthz${version:+ with $version}."
        return 0
    fi
    hub_warn "the Hub did not answer /healthz${version:+ with $version} within $HUB_START_TIMEOUT s; see $LOGS/hub.log and journalctl --user -u $HUB_UNIT"
    return 1
}

hub_status_names() {  # status.json names this commit as its build
    [[ $(hub_json_field "$STATE/status.json" build) == "$1" ]]
}

hub_unit_property() { systemctl --user show "$HUB_UNIT" -p "$1" --value 2>/dev/null || true; }

# The deployment check: the unit is enabled, running and ordered on no other service; /healthz answers ok with the
# deployed version; status.json is fresh and names the deployed commit. Prints one line per check and fails if any does.
# Deploy and rollback end with it.
hub_check() {
    local failed=0 enabled active started result deps dep linger commit version written written_epoch age=0 max_age
    check_ok() { printf 'ok    %s\n' "$*"; }
    check_fail() {
        printf 'FAIL  %s\n' "$*"
        failed=1
    }

    enabled=$(systemctl --user is-enabled "$HUB_UNIT" 2>/dev/null) || true
    if [[ $enabled == enabled ]]; then
        check_ok "$HUB_UNIT is enabled"
    else
        check_fail "$HUB_UNIT is ${enabled:-not installed}, so it does not start at boot (install-unit.sh installs it)"
    fi

    active=$(systemctl --user is-active "$HUB_UNIT" 2>/dev/null) || true
    if [[ $active == active ]]; then
        check_ok "$HUB_UNIT is active"
    else
        started=$(hub_unit_property ExecMainStartTimestampMonotonic)
        result=$(hub_unit_property Result)
        if [[ $active == activating ]]; then
            check_fail "$HUB_UNIT is restarting after the Hub exited (Restart=always); see $LOGS/hub.log and" \
                "journalctl --user -u $HUB_UNIT"
        elif [[ $active == failed ]]; then
            check_fail "$HUB_UNIT failed (Result=${result:-unknown}); see journalctl --user -u $HUB_UNIT"
        elif [[ $enabled == enabled && ${started:-0} == 0 ]]; then
            # The 2026-10-29 failure: an ordering cycle made systemd delete the start job at boot, and nothing recorded
            # a failure. The unit is enabled, inactive, and has not started once since the user manager came up.
            check_fail "$HUB_UNIT is enabled but has not started since the user manager came up: its start job was" \
                "dropped at boot (an ordering cycle does this without recording a failure; look for 'ordering cycle'" \
                "in journalctl --user -b). Start it with: systemctl --user start $HUB_UNIT"
        else
            check_fail "$HUB_UNIT is ${enabled:-not installed} but ${active:-not running}: it was stopped." \
                "Start it with: systemctl --user start $HUB_UNIT"
        fi
    fi

    deps="$(hub_unit_property After) $(hub_unit_property Requires) $(hub_unit_property BindsTo) $(hub_unit_property Requisite)"
    for dep in $deps; do
        if [[ $dep == *.service ]]; then
            check_fail "$HUB_UNIT is ordered on or requires $dep; ordering on another service, such as the daemon's, can" \
                "form a cycle that drops the Hub's start job at boot. Reinstall the unit with install-unit.sh"
        fi
    done

    if command -v loginctl >/dev/null; then
        linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null) || true
        if [[ $linger == no ]]; then
            hub_warn "lingering is off for $(id -un): the user manager, and the Hub with it, starts only at a login" \
                "(loginctl enable-linger $(id -un))"
        fi
    fi

    commit=$(hub_json_field "$STAMP" commit)
    version=$(hub_json_field "$STAMP" version)
    if [[ -z $version ]]; then
        check_fail "no deployment record: $STAMP is missing or unreadable (deploy-hub.sh writes it)"
    elif hub_health "$version"; then
        check_ok "/healthz on $HUB_HOST:$PORT answers ok with version $version"
    else
        check_fail "/healthz on $HUB_HOST:$PORT does not answer ok with the deployed version $version"
    fi

    if ((STATUS_SECONDS == 0)); then
        check_fail "the status interval is 0, so the Hub writes no status.json (HUB_STATUS_SECONDS in $ENV_FILE)"
    else
        max_age=${HUB_STATUS_MAX_AGE:-$((STATUS_SECONDS * 5))}
        written=$(hub_json_field "$STATE/status.json" written_at)
        written_epoch=''
        [[ -n $written ]] && written_epoch=$(date -u -d "$written" +%s 2>/dev/null || true)
        [[ -n $written_epoch ]] && age=$(($(date -u +%s) - written_epoch))
        if [[ -z $written ]]; then
            check_fail "$STATE/status.json is missing or has no written_at"
        elif [[ -z $written_epoch ]]; then
            check_fail "$STATE/status.json has an unreadable written_at: $written"
        elif ((age > max_age)); then
            check_fail "$STATE/status.json is stale: written $age s ago (more than $max_age s)"
        else
            check_ok "status.json was written $age s ago"
        fi
        if [[ -n $commit ]] && ! hub_status_names "$commit"; then
            check_fail "status.json names build $(hub_json_field "$STATE/status.json" build), not the deployed $commit"
        fi
    fi
    unset -f check_ok check_fail
    return "$failed"
}
