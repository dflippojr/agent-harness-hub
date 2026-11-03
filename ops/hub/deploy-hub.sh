#!/usr/bin/env bash
# Deploys one commit of the Hub on a Linux host, or rolls back to the previous deployment. The Linux counterpart of
# deploy-hub.ps1, fail-closed the same way.
#
# Usage: deploy-hub.sh [--commit SHA] [--release-root DIR] [--deploy-dir DIR] [--python PYTHON]
#                      [--port N] [--daemon-url URL] [--public-url https://...] [--status-seconds N] [--replace-unit]
#                      [--allow-unmerged] [--dry-run]
#        deploy-hub.sh --rollback [--deploy-dir DIR] [--dry-run]
#
# It builds the requested commit (from `git archive`, so the working tree never matters) into a side-by-side virtual
# environment with the locked, hash-checked requirements before any downtime, checks that environment reports a version
# and carries the UI, and only then stops the Hub, repoints .venv-path, writes deployed.json, copies ops/, installs the
# systemd user unit (install-unit.sh), starts it and runs the check (check-hub.sh). A failure after the stop restores the
# previous pointers, records and scripts and restarts the previous Hub; a failed deployment never becomes "previous". It
# keeps the previous environment for --rollback and deletes older ones.
#
# By default the commit must already be on origin/main. --allow-unmerged is for a trial deployment of a pull request's
# head; it is recorded in deployed.json and printed, and the next deployment from main replaces it.
#
# It never touches the agent-harness daemon and publishes nothing: the Hub listens on loopback only (hub-common.sh).
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=hub-common.sh
source "$here/hub-common.sh"
# shellcheck source=hub-ops.sh
source "$here/hub-ops.sh"

usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
COMMIT='' RELEASE_ROOT='' deploy_dir='' SEED_PYTHON='' ALLOW_UNMERGED=0 ROLLBACK=0 DRY_RUN=0
PORT='' DAEMON_URL='' PUBLIC_URL='' STATUS_SECONDS='' unit_args=()
while (($#)); do
    case $1 in
        --commit) COMMIT=${2:?--commit needs a value}; shift 2 ;;
        --release-root) RELEASE_ROOT=${2:?--release-root needs a value}; shift 2 ;;
        --deploy-dir) deploy_dir=${2:?--deploy-dir needs a value}; shift 2 ;;
        --python) SEED_PYTHON=${2:?--python needs a value}; shift 2 ;;
        --port | --daemon-url | --status-seconds) unit_args+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        --public-url) unit_args+=("$1" "${2?$1 needs a value}"); shift 2 ;;
        --replace-unit) unit_args+=("$1"); shift ;;
        --allow-unmerged) ALLOW_UNMERGED=1; shift ;;
        --rollback) ROLLBACK=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) hub_die "unknown argument: $1 (see --help)" ;;
    esac
done
[[ -z $COMMIT || $COMMIT =~ ^[0-9a-f]{40}$ ]] || hub_die "--commit must be a full 40-character lowercase SHA: $COMMIT"
hub_set_paths "${deploy_dir:-$(hub_default_deploy_dir)}"

run() {
    if ((DRY_RUN)); then
        printf '[dry run] %s\n' "$*"
        return 0
    fi
    "$@"
}

# Saved copies of the four records (and, for a deployment, the unit file and hub.env, which install-unit.sh rewrites),
# put back as they were (present or absent) when a step fails.
SNAPSHOT=$DEPLOY/.snapshot
SNAPSHOT_FILES=("$POINTER" "$PREVIOUS" "$STAMP" "$STAMP_OLD")
snapshot_records() {
    local file name
    rm -rf -- "$SNAPSHOT" && mkdir -p -- "$SNAPSHOT" || return 1
    for file in "${SNAPSHOT_FILES[@]}"; do
        name=$SNAPSHOT/$(basename -- "$file")
        if [[ -f $file ]]; then cp -p -- "$file" "$name" || return 1; else : >"$name.absent" || return 1; fi
    done
}
restore_records() {
    local file name ok=0
    for file in "${SNAPSHOT_FILES[@]}"; do
        name=$SNAPSHOT/$(basename -- "$file")
        if [[ -e $name.absent ]]; then
            rm -f -- "$file" || ok=1
        else
            { cp -p -- "$name" "$file.tmp" && mv -f -- "$file.tmp" "$file"; } || ok=1
        fi
    done
    return "$ok"
}
saved_record() { [[ -f $SNAPSHOT/$1 ]]; }  # the snapshot holds a copy of this record (it existed)

write_stamp() {  # path commit version unmerged(true|false)
    python3 -I -c '
import json, sys, time
path, commit, version, unmerged = sys.argv[1:]
stamp = {"commit": commit, "version": version, "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "unmerged": unmerged == "true"}
with open(path + ".tmp", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(stamp, indent=2) + "\n")
' "$@" && mv -f -- "$1.tmp" "$1"
}

if ((!DRY_RUN)); then
    mkdir -p -- "$DEPLOY" "$VENVS" "$LOGS"
    # One deployment or rollback at a time.
    exec 9>"$DEPLOY/.deploy.lock"
    flock -n 9 || hub_die "another deployment or rollback is running ($DEPLOY/.deploy.lock)"
fi
hub_load_env

# -- rollback ---------------------------------------------------------------------------------------------------------
if ((ROLLBACK)); then
    current=$(hub_read_pointer "$POINTER")
    previous=$(hub_read_pointer "$PREVIOUS")
    [[ -n $previous && -d $previous ]] || hub_die "no previous deployment to roll back to ($PREVIOUS)"
    if ((DRY_RUN)); then
        hub_say '[dry run] stop the Hub, swap .venv-path, deployed.json and ops/ with their previous copies, start, check'
        exit 0
    fi
    snapshot_records || hub_die "could not save the deployment records"
    hub_stop || hub_die "could not stop the Hub; nothing was changed"
    OPS_SWAPPED=0
    rollback_swap() {
        hub_write_file "$POINTER" "$previous" || return 1
        hub_write_file "$PREVIOUS" "$current" || return 1
        swap_ops || return 1
        OPS_SWAPPED=1
        if saved_record deployed.previous.json; then cp -- "$SNAPSHOT/deployed.previous.json" "$STAMP" || return 1; fi
        if saved_record deployed.json; then cp -- "$SNAPSHOT/deployed.json" "$STAMP_OLD" || return 1; fi
        hub_start "$(hub_json_field "$STAMP" version)" || return 1
        hub_wait "$HUB_START_TIMEOUT" hub_status_names "$(hub_json_field "$STAMP" commit)" ||
            hub_warn 'status.json did not name the restored commit in time'
        hub_check
    }
    if rollback_swap; then
        rm -rf -- "$SNAPSHOT"
        hub_say "Rolled back to $previous"
        exit 0
    fi
    # Stop first: the Hub reads its environment once, so only a new start runs the restored one.
    errors=()
    hub_stop || errors+=("stop failed")
    if ((OPS_SWAPPED)); then swap_ops || errors+=("ops swap-back failed"); fi
    restore_records || errors+=("restoring the records failed")
    hub_start "$(hub_json_field "$STAMP" version)" || errors+=("restart failed")
    rm -rf -- "$SNAPSHOT"
    ((${#errors[@]} == 0)) || hub_die "rollback failed; restoring what ran before also failed: ${errors[*]}"
    hub_die "rollback failed; the previous state is restored"
fi

# -- guards -----------------------------------------------------------------------------------------------------------
release=$(cd -- "${RELEASE_ROOT:-$here/../..}" && pwd) || hub_die "no release checkout: ${RELEASE_ROOT:-$here/../..}"
[[ -e $release/.git ]] || hub_die "release checkout is not a git repository: $release"
head=$(git -C "$release" rev-parse HEAD) || hub_die "could not resolve the release checkout HEAD"
COMMIT=${COMMIT:-$head}
[[ $head == "$COMMIT" ]] || hub_die "release checkout is at $head, expected $COMMIT"
dirty=$(git -C "$release" status --porcelain --untracked-files=no) || hub_die "could not inspect the release checkout"
[[ -z $dirty ]] || hub_die "release checkout has uncommitted changes; refusing deployment:"$'\n'"$dirty"
unmerged=false
run git -C "$release" fetch --quiet --prune origin main || hub_die "git fetch origin main failed"
if ((!DRY_RUN)) && ! git -C "$release" merge-base --is-ancestor "$COMMIT" origin/main; then
    ((ALLOW_UNMERGED)) || hub_die "$COMMIT is not on origin/main; deploy a merged commit (or pass --allow-unmerged for a trial)"
    unmerged=true
    hub_warn "deploying $COMMIT, which is not on origin/main (--allow-unmerged)"
fi
hub_say '[deploy] guards passed'

# -- stage the environment before any downtime ------------------------------------------------------------------------
current_venv=$(hub_read_pointer "$POINTER")
if [[ -z $SEED_PYTHON ]]; then
    if [[ -n $current_venv && -x $current_venv/bin/python ]]; then SEED_PYTHON=$current_venv/bin/python; else SEED_PYTHON=python3; fi
fi
staged=$VENVS/$COMMIT
[[ ! -e $staged ]] || hub_die "staged environment already exists: $staged (is $COMMIT already deployed?)"
source_dir=$DEPLOY/.source-$COMMIT
version=dry-run
stage() {
    local python=$staged/bin/python reported static
    local pip=(-m pip install --disable-pip-version-check --no-input)
    run mkdir -p -- "$source_dir" || return 1
    if ((DRY_RUN)); then
        printf '[dry run] git -C %s archive %s | tar -x -C %s\n' "$release" "$COMMIT" "$source_dir"
    else
        git -C "$release" archive --format=tar "$COMMIT" | tar -x -C "$source_dir" || return 1
    fi
    run "$SEED_PYTHON" -m venv "$staged" || return 1
    run "$python" "${pip[@]}" --require-hashes -r "$source_dir/requirements.lock" \
        -r "$source_dir/requirements-build.lock" || return 1
    run "$python" "${pip[@]}" --no-deps --no-build-isolation "$source_dir" || return 1
    ((DRY_RUN)) && return 0
    reported=$("$python" -I -m harness_hub --version) || { hub_warn "staged Hub did not report a version"; return 1; }
    [[ $reported =~ ^harness_hub\ ([A-Za-z0-9.+_-]+)$ ]] || { hub_warn "staged Hub did not report a version: $reported"; return 1; }
    version=${BASH_REMATCH[1]}
    static=$("$python" -I -c "from importlib import resources; s = resources.files('harness_hub') / 'static'; print((s / 'index.html').is_file() and (s / 'pages' / 'overview.mjs').is_file())") || return 1
    [[ $static == True ]] || { hub_warn 'staged Hub carries no UI'; return 1; }
}
if ! stage; then
    ((DRY_RUN)) || rm -rf -- "$staged" "$source_dir"
    hub_die "staging $COMMIT failed; nothing else was changed"
fi
((DRY_RUN)) || rm -rf -- "$source_dir"
hub_say "[deploy] Hub $version staged at $staged"

# -- swap, start, check; roll back on failure -------------------------------------------------------------------------
previous_venv=$current_venv
previous_of_previous=$(hub_read_pointer "$PREVIOUS")
if ((DRY_RUN)); then
    hub_say '[dry run] copy ops; install the unit; stop the Hub; repoint .venv-path; write deployed.json; start; check'
    exit 0
fi
SNAPSHOT_FILES+=("$UNIT_FILE" "$ENV_FILE")
snapshot_records || hub_die "could not save the deployment records"
# What ran before: a Hub from a unit this script did not write (no .venv-path yet) is restarted on failure too.
UNIT_WAS_ENABLED=0 HUB_WAS_ACTIVE=0
if hub_unit_installed; then
    [[ $(systemctl --user is-enabled "$HUB_UNIT" 2>/dev/null || true) == enabled ]] && UNIT_WAS_ENABLED=1
    [[ $(systemctl --user is-active "$HUB_UNIT" 2>/dev/null || true) == active ]] && HUB_WAS_ACTIVE=1
fi
# Puts the unit file, its enablement and hub.env back as they were before install-unit.sh.
restore_unit() {
    local ok=0
    if [[ -e $SNAPSHOT/$HUB_UNIT.absent ]] || ((!UNIT_WAS_ENABLED)); then
        if hub_unit_installed; then systemctl --user disable "$HUB_UNIT" >/dev/null 2>&1 || ok=1; fi
    fi
    restore_records || ok=1
    systemctl --user daemon-reload || ok=1
    if ((UNIT_WAS_ENABLED)); then systemctl --user enable "$HUB_UNIT" >/dev/null 2>&1 || ok=1; fi
    return "$ok"
}
STOPPED=0
deploy_swap() {
    copy_ops "$here" || return 1
    bash "$DEPLOY/ops/install-unit.sh" --deploy-dir "$DEPLOY" "${unit_args[@]}" || return 1
    # install-unit.sh may have changed hub.env (--port and the rest): the check reads it again.
    hub_reload_env
    hub_stop || return 1
    STOPPED=1
    if [[ -n $previous_venv ]]; then hub_write_file "$PREVIOUS" "$previous_venv" || return 1; fi
    if saved_record deployed.json; then cp -- "$SNAPSHOT/deployed.json" "$STAMP_OLD" || return 1; fi
    hub_write_file "$POINTER" "$staged" || return 1
    write_stamp "$STAMP" "$COMMIT" "$version" "$unmerged" || return 1
    hub_say '[deploy] environment swapped'
    hub_start "$version" || return 1
    # The Hub's own heartbeat must name this commit too, not just answer.
    hub_wait "$HUB_START_TIMEOUT" hub_status_names "$COMMIT" ||
        { hub_warn "status.json did not name $COMMIT within $HUB_START_TIMEOUT s"; return 1; }
    hub_check || return 1
}
if ! deploy_swap; then
    if ((!STOPPED)); then
        undo_ops
        restore_unit || hub_warn "restoring the unit file or hub.env failed"
        rm -rf -- "$staged" "$SNAPSHOT"
        hub_die "deployment failed before the Hub was stopped; nothing else was changed"
    fi
    errors=()
    hub_stop || errors+=("stop failed")
    undo_ops || errors+=("restoring ops failed")
    restore_unit || errors+=("restoring the records, the unit file or hub.env failed")
    hub_reload_env
    if [[ -n $previous_venv ]] || ((HUB_WAS_ACTIVE)); then
        # A Hub this script did not deploy has no deployed.json: then any healthy answer will do.
        if hub_start "$(hub_json_field "$STAMP" version)"; then
            hub_say '[rollback] previous Hub started and healthy'
        else
            errors+=("restarting the previous Hub failed")
        fi
    fi
    rm -rf -- "$staged" || errors+=("removing $staged failed")
    rm -rf -- "$SNAPSHOT"
    ((${#errors[@]} == 0)) || hub_die "deployment failed after the stop; rollback also failed: ${errors[*]}"
    hub_die "deployment failed after the stop; the previous deployment is restored"
fi

# The deployment stands: ops.previous goes with the environment it replaced, and the older copy is dropped.
complete_ops
rm -rf -- "$SNAPSHOT"
# Keep the environment this replaced for --rollback; delete the one before it.
if [[ -n $previous_of_previous && $previous_of_previous != "$staged" && $previous_of_previous != "$previous_venv" &&
    $previous_of_previous == "$VENVS"/* ]]; then
    rm -rf -- "$previous_of_previous" || hub_warn "old environment cleanup failed: $previous_of_previous"
fi
hub_say "Deployed $COMMIT (Hub $version)"
hub_say "  environment: $staged"
hub_say "  previous:    ${previous_venv:-none}"
