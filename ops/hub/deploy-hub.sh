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
# The environment is built with --python, else the interpreter of the deployed Hub's environment (.venv-path, or on a
# first deployment the one a running Hub or a hand-written unit's ExecStart names), else python3: the first of them
# that can create a virtual environment (it needs ensurepip: Debian's python3.X-venv package). None that can is an error
# before anything changes.
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

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
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

# The check, run as its own process from the deployment's scripts: check-hub.sh has set -e at its top level, so an
# error inside the check ends it with a failure instead of being ignored here, where the call is part of a condition
# (Bash turns errexit off there). It passes only on exit 0 with its closing "all passed" line.
run_check() {
    local output status=0
    output=$(bash "$DEPLOY/ops/check-hub.sh" --deploy-dir "$DEPLOY" 2>&1) || status=$?
    printf '%s\n' "$output"
    [[ $status == 0 && $output == *$'\n''check: all passed' ]]
}

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
    if ! hub_stop; then
        # A stop can take effect and still report an error: never assume the Hub is still running. Nothing else has
        # changed yet, so start what was running and say which state it is in.
        rm -rf -- "$SNAPSHOT"
        if hub_restore_start; then
            hub_die "rollback not done: stopping the Hub reported an error; nothing was changed and the current Hub" \
                "answers /healthz again"
        fi
        hub_die "rollback not done: stopping the Hub reported an error, and the current Hub does not answer /healthz" \
            "now; nothing else was changed. Start it with: systemctl --user start $HUB_UNIT"
    fi
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
        run_check
    }
    if rollback_swap; then
        rm -rf -- "$SNAPSHOT"
        hub_say "Rolled back to $previous"
        exit 0
    fi
    # Stop first: the Hub reads its environment once, so only a new start runs the restored one. A stop that reports
    # an error may still have stopped it, so the restore and the restart run either way; only the restart's result
    # decides whether what ran before is back.
    errors=() notes=()
    hub_stop || notes+=("stopping the failed rollback reported an error")
    if ((OPS_SWAPPED)); then swap_ops || errors+=("swapping ops/ back failed"); fi
    restore_records || errors+=("restoring the records failed")
    hub_restore_start || errors+=("the Hub that ran before is not confirmed running")
    rm -rf -- "$SNAPSHOT"
    ((${#errors[@]} == 0)) || hub_die "rollback failed; restoring what ran before also failed: ${errors[*]}"
    hub_die "rollback failed; the previous state is restored and its Hub answers /healthz${notes[*]:+ (${notes[*]})}"
fi

# -- the interpreter the environment is built with --------------------------------------------------------------------
# A virtual environment's interpreter (an absolute <venv>/bin/python*, with <venv>/pyvenv.cfg), printed, or a failure.
venv_interpreter() {
    local path=$1
    [[ $path == /* && $(basename -- "$path") == python* && -x $path ]] || return 1
    [[ -f $(dirname -- "$(dirname -- "$path")")/pyvenv.cfg ]] || return 1
    printf '%s\n' "$path"
}
# The words of a systemd command line, each ended by a NUL, unquoted and with systemd's C escapes (\x20, \s, \\ ...)
# decoded. As in a shell, a word is a run of "..." and '...' parts (which may hold spaces) and unquoted text.
unit_words() {
    local rest=$1 word
    local double='^"((\\.|[^"\\])*)"(.*)$' single="^'((\\\\.|[^'\\\\])*)'(.*)\$"
    local bare='^((\\.|[^[:space:]"'"'"'\\])+)(.*)$' space='^[[:space:]]'
    rest=${rest#"${rest%%[![:space:]]*}"}
    while [[ -n $rest ]]; do
        word=''
        # The parts of one word, up to the whitespace after it.
        while [[ -n $rest && ! $rest =~ $space ]]; do
            [[ $rest =~ $double || $rest =~ $single || $rest =~ $bare ]] || return 0  # an unterminated quote
            rest=${BASH_REMATCH[3]}
            unit_unescape "${BASH_REMATCH[1]}"  # (it uses BASH_REMATCH itself)
            word+=$UNESCAPED
        done
        printf '%s\0' "$word"
        rest=${rest#"${rest%%[![:space:]]*}"}
    done
}
# systemd's C escapes decoded in one pass, left to right, into UNESCAPED: \xHH, \NNN (octal), \uXXXX, \UXXXXXXXX, \s,
# \n \t \r \a \b \f \v, and a backslash before any other character (\\, \", \') stands for that character.
unit_unescape() {
    local rest=$1 char LC_ALL=C.UTF-8  # \u and \U decode to UTF-8, as systemd does, whatever the caller's locale
    local hex='^\\x([0-9a-fA-F]{2})(.*)$' octal='^\\([0-7]{3})(.*)$' unicode='^\\u([0-9a-fA-F]{4})(.*)$'
    local wide='^\\U([0-9a-fA-F]{8})(.*)$'
    local other='^\\(.)(.*)$'
    UNESCAPED=''
    while [[ -n $rest ]]; do
        if [[ $rest =~ $hex ]]; then
            printf -v char %b "\\x${BASH_REMATCH[1]}"
        elif [[ $rest =~ $octal ]]; then
            printf -v char %b "\\0${BASH_REMATCH[1]}"
        elif [[ $rest =~ $wide ]]; then
            printf -v char %b "\\U${BASH_REMATCH[1]}"
        elif [[ $rest =~ $unicode ]]; then
            printf -v char %b "\\u${BASH_REMATCH[1]}"
        elif [[ $rest =~ $other ]]; then
            case ${BASH_REMATCH[1]} in
                s) char=' ' ;; n) char=$'\n' ;; t) char=$'\t' ;; r) char=$'\r' ;;
                a) char=$'\a' ;; b) char=$'\b' ;; f) char=$'\f' ;; v) char=$'\v' ;;
                *) char=${BASH_REMATCH[1]} ;;
            esac
        else
            UNESCAPED+=${rest:0:1} rest=${rest:1}
            continue
        fi
        UNESCAPED+=$char rest=${BASH_REMATCH[2]}
    done
}
# The command lines the unit's [Service] ExecStart= runs, each ended by a NUL, as systemd reads them: a line ending in \
# continues on the next, comment lines are skipped (also inside a continued one), and an empty ExecStart= clears the ones before it.
unit_exec_starts() {
    local line logical='' section='' commands=()
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}  # a unit written on Windows
        # A comment line is skipped, even between the lines of a continued one.
        [[ $line =~ ^[[:space:]]*[\#\;] ]] && continue
        # An odd number of trailing backslashes continues the line; an even number is escaped backslashes.
        if [[ $line =~ (^|[^\\])(\\\\)*\\$ ]]; then logical+="${line%\\} "; continue; fi
        line=$logical$line logical=''
        if [[ $line =~ ^[[:space:]]*\[(.*)\][[:space:]]*$ ]]; then section=${BASH_REMATCH[1]}; continue; fi
        [[ $section == Service && $line =~ ^[[:space:]]*ExecStart[[:space:]]*=[[:space:]]*(.*)$ ]] || continue
        if [[ -z ${BASH_REMATCH[1]} ]]; then commands=(); else commands+=("${BASH_REMATCH[1]}"); fi
    done <"$UNIT_FILE"
    ((${#commands[@]} == 0)) || printf '%s\0' "${commands[@]}"
}
# A word with the systemd specifiers a user unit's interpreter path may hold expanded: %h (home), %u (user name),
# %U (user id) and %% (a percent sign). Any other specifier is left as it is, so that path matches no interpreter.
unit_specifiers() {
    local rest=$1 out=''
    while [[ $rest == *%* ]]; do
        out+=${rest%%\%*} rest=${rest#*%}
        case ${rest:0:1} in
            h) out+=$HOME ;; u) out+=$(id -un) ;; U) out+=$(id -u) ;; %) out+=% ;;
            *) out+=%${rest:0:1} ;;
        esac
        rest=${rest:1}
    done
    printf '%s\n' "$out$rest"
}
# The interpreters to try, in order, with where each came from. --python alone when given. Otherwise the deployed
# environment's: .venv-path's; or, for a Hub this script did not deploy (no .venv-path yet), the first argument of a
# running Hub on this state directory (read from /proc, where argument boundaries survive), then the venv interpreters
# the hand-written unit's ExecStart= names. python3 last.
CANDIDATES=() CANDIDATE_SOURCES=()
add_candidate() { CANDIDATES+=("$1") CANDIDATE_SOURCES+=("$2"); }
current_venv=$(hub_read_pointer "$POINTER")
if [[ -n $SEED_PYTHON ]]; then
    add_candidate "$SEED_PYTHON" --python
else
    if [[ -n $current_venv ]]; then
        [[ -x $current_venv/bin/python ]] && add_candidate "$current_venv/bin/python" 'the deployed environment, .venv-path'
    else
        for pid in $(hub_processes); do
            IFS= read -r -d '' word <"/proc/$pid/cmdline" 2>/dev/null || continue
            word=$(venv_interpreter "$word") && add_candidate "$word" "the running Hub's environment"
        done
        if [[ -f $UNIT_FILE ]]; then
            while IFS= read -r -d '' command; do
                first=1
                while IFS= read -r -d '' word; do
                    # systemd's prefixes (-, @, :, +, !, |) on the first word, after it is unquoted.
                    ((first)) && [[ $word =~ ^[-@:+!|]+(.*)$ ]] && word=${BASH_REMATCH[1]}
                    first=0
                    word=$(venv_interpreter "$(unit_specifiers "$word")") && add_candidate "$word" "the environment $UNIT_FILE runs"
                done < <(unit_words "$command")
            done < <(unit_exec_starts)
        fi
    fi
    add_candidate python3 'the default'
fi
# The first that can create a virtual environment. ensurepip is what `-m venv` needs and what Debian and Ubuntu leave
# out of the system Python (python3.X-venv).
SEED_PYTHON='' tried=()
for index in "${!CANDIDATES[@]}"; do
    if "${CANDIDATES[index]}" -I -c 'import ensurepip, venv' >/dev/null 2>&1; then
        SEED_PYTHON=${CANDIDATES[index]} python_source=${CANDIDATE_SOURCES[index]}
        break
    fi
    tried+=("${CANDIDATES[index]} (${CANDIDATE_SOURCES[index]})")
done
printf -v tried_list '%s; ' "${tried[@]}"
if [[ -z $SEED_PYTHON ]]; then
    hub_die "no interpreter can create a virtual environment; tried: ${tried_list%; }." \
        "Each is missing or has no ensurepip (on Debian and Ubuntu, install python3-venv for that version). Pass" \
        "--python with an interpreter that can, such as the Python 3.12 the Hub's CI uses (docs/deployment.md);" \
        "nothing was changed"
fi
((${#tried[@]} == 0)) || hub_warn "skipped, as they cannot create a virtual environment: ${tried_list%; }"
hub_say "[deploy] building the environment with $SEED_PYTHON ($python_source)"

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
staged=$VENVS/$COMMIT
[[ ! -e $staged ]] || hub_die "staged environment already exists: $staged (is $COMMIT already deployed?)"
source_dir=$DEPLOY/.source-$COMMIT
# The scripts deployed with the Hub are the commit's own, from the archive: not the copy running this script, which may
# be the deployed one (<deploy-dir>/ops) of an older commit.
ops_source=$DEPLOY/.ops-$COMMIT
version=dry-run
stage() {
    local python=$staged/bin/python reported static
    local pip=(-m pip install --disable-pip-version-check --no-input)
    run mkdir -p -- "$source_dir" || return 1
    if ((DRY_RUN)); then
        printf '[dry run] git -C %s archive %s | tar -x -C %s\n' "$release" "$COMMIT" "$source_dir"
    else
        git -C "$release" archive --format=tar "$COMMIT" | tar -x -C "$source_dir" || return 1
        [[ -f $source_dir/ops/hub/deploy-hub.sh && -f $source_dir/ops/hub/run-hub.sh ]] ||
            { hub_warn "$COMMIT carries no Linux deployment scripts (ops/hub/*.sh)"; return 1; }
        rm -rf -- "$ops_source" && mkdir -p -- "$ops_source" && cp -- "$source_dir"/ops/hub/*.sh "$ops_source"/ || return 1
    fi
    run "$SEED_PYTHON" -m venv "$staged" ||
        { hub_warn "$SEED_PYTHON could not create $staged; choose another interpreter with --python"; return 1; }
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
    ((DRY_RUN)) || rm -rf -- "$staged" "$source_dir" "$ops_source"
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
# Set before the stop is attempted, not after it succeeds: a stop can take effect and still report an error, and from then
# on the failure path must restore and restart what ran before.
STOP_ATTEMPTED=0
deploy_swap() {
    copy_ops "$ops_source" || return 1
    bash "$DEPLOY/ops/install-unit.sh" --deploy-dir "$DEPLOY" "${unit_args[@]}" || return 1
    # install-unit.sh may have changed hub.env (--port and the rest): the check reads it again.
    hub_reload_env
    STOP_ATTEMPTED=1
    hub_stop || return 1
    if [[ -n $previous_venv ]]; then hub_write_file "$PREVIOUS" "$previous_venv" || return 1; fi
    if saved_record deployed.json; then cp -- "$SNAPSHOT/deployed.json" "$STAMP_OLD" || return 1; fi
    hub_write_file "$POINTER" "$staged" || return 1
    write_stamp "$STAMP" "$COMMIT" "$version" "$unmerged" || return 1
    hub_say '[deploy] environment swapped'
    hub_start "$version" || return 1
    # The Hub's own heartbeat must name this commit too, not just answer.
    hub_wait "$HUB_START_TIMEOUT" hub_status_names "$COMMIT" ||
        { hub_warn "status.json did not name $COMMIT within $HUB_START_TIMEOUT s"; return 1; }
    run_check || return 1
}
if ! deploy_swap; then
    if ((!STOP_ATTEMPTED)); then
        # Every restore step runs, whatever an earlier one returned (a bare failing call would end the script under
        # set -e and skip the rest); the failures are collected and reported together.
        errors=()
        undo_ops || errors+=("restoring ops/ and ops.previous/ failed (see $DEPLOY; ops.previous.bak may hold the older copy)")
        restore_unit || errors+=("restoring the records, the unit file or hub.env failed")
        rm -rf -- "$staged" "$SNAPSHOT" "$ops_source" || errors+=("removing $staged failed")
        ((${#errors[@]} == 0)) ||
            hub_die "deployment failed before the Hub was stopped (it still runs); restoring failed: ${errors[*]}"
        hub_die "deployment failed before the Hub was stopped; nothing else was changed"
    fi
    # The stop was attempted, so the Hub may be down whatever hub_stop returned: stop whatever runs now, restore
    # everything, and start what ran before. Only the restart's result decides whether it is back.
    errors=() notes=() restarted=0
    hub_stop || notes+=("stopping the Hub reported an error")
    undo_ops || errors+=("restoring ops/ and ops.previous/ failed (see $DEPLOY; ops.previous.bak may hold the older copy)")
    restore_unit || errors+=("restoring the records, the unit file or hub.env failed")
    hub_reload_env
    if [[ -n $previous_venv ]] || ((HUB_WAS_ACTIVE)); then
        if hub_restore_start; then
            restarted=1
            hub_say '[rollback] previous Hub started and healthy'
        else
            errors+=("the previous Hub is not confirmed running")
        fi
    fi
    if ((${#errors[@]} == 0)); then
        rm -rf -- "$staged" || errors+=("removing $staged failed")
    else
        # It may be what is running now: keep it, and say so.
        errors+=("kept $staged, which may still be running")
    fi
    rm -rf -- "$SNAPSHOT" "$ops_source"
    ((${#errors[@]} == 0)) || hub_die "deployment failed after the Hub was stopped; restoring also failed: ${errors[*]}"
    if ((restarted)); then
        hub_die "deployment failed after the Hub was stopped; the previous deployment is restored and answers" \
            "/healthz${notes[*]:+ (${notes[*]})}"
    fi
    hub_die "deployment failed after the Hub was stopped; the previous records are restored, and no Hub was running" \
        "before, so none was started${notes[*]:+ (${notes[*]})}"
fi

# The deployment stands: ops.previous goes with the environment it replaced, and the older copy is dropped. A failure to
# delete that copy does not undo a deployment that passed its check.
complete_ops || hub_warn "could not delete $DEPLOY/ops.previous.bak; delete it yourself"
rm -rf -- "$SNAPSHOT" "$ops_source"
# Keep the environment this replaced for --rollback; delete the one before it.
if [[ -n $previous_of_previous && $previous_of_previous != "$staged" && $previous_of_previous != "$previous_venv" &&
    $previous_of_previous == "$VENVS"/* ]]; then
    rm -rf -- "$previous_of_previous" || hub_warn "old environment cleanup failed: $previous_of_previous"
fi
hub_say "Deployed $COMMIT (Hub $version)"
hub_say "  environment: $staged"
hub_say "  previous:    ${previous_venv:-none}"
