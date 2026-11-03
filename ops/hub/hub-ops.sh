# shellcheck shell=bash
# The deployed copy of the scripts (<deploy-dir>/ops) and the copy it replaced (ops.previous), which travel with the
# environments in .venv-path and .venv-previous. Sourced by deploy-hub.sh; needs DEPLOY (hub_set_paths) and may read
# DRY_RUN. The Linux counterpart of hub-ops.ps1, kept apart so tests can replay it on a scratch directory.

ops_remove_tree() {
    if [[ ${DRY_RUN:-0} == 1 ]]; then
        printf '[dry run] rm -rf %s\n' "$1"
        return 0
    fi
    rm -rf -- "$1"
}

# The unit runs the deployment's own copy of these scripts, so a checkout change can't alter a running Hub. The copy
# this replaces becomes ops.previous, so a rollback restores the scripts that went with the old Hub. The ops.previous it
# displaces is parked as ops.previous.bak until complete_ops, so undo_ops can put every copy back when the deployment
# fails: a failed deployment is never left as "previous".
#
# The new copy is made in full (ops.new) before anything moves, so the source may be ops/ itself: a deploy-hub.sh run
# from the deployed copy would otherwise move its own directory away and then copy from the empty path.
copy_ops() {
    local source=$1 target=$DEPLOY/ops old=$DEPLOY/ops.previous parked=$DEPLOY/ops.previous.bak fresh=$DEPLOY/ops.new
    if [[ ${DRY_RUN:-0} == 1 ]]; then
        printf '[dry run] copy %s/*.sh -> %s; ops -> ops.previous\n' "$source" "$target"
        return 0
    fi
    OPS_UNDO=1 OPS_PARKED=0 OPS_MOVED=0 OPS_CREATED=0
    ops_remove_tree "$fresh" || return 1
    { mkdir -p -- "$fresh" && cp -- "$source"/*.sh "$fresh"/ && chmod 0755 "$fresh"/*.sh; } ||
        { ops_remove_tree "$fresh"; return 1; }
    ops_remove_tree "$parked" || return 1
    if [[ -e $old ]]; then mv -- "$old" "$parked" || return 1; OPS_PARKED=1; fi
    if [[ -e $target ]]; then mv -- "$target" "$old" || return 1; OPS_MOVED=1; fi
    mv -- "$fresh" "$target" || return 1
    OPS_CREATED=1
}

# Puts ops and ops.previous back as they were before copy_ops (no ops/ at all, on a first install). Does nothing if
# copy_ops did not start.
undo_ops() {
    [[ ${OPS_UNDO:-0} == 1 ]] || return 0
    local target=$DEPLOY/ops old=$DEPLOY/ops.previous parked=$DEPLOY/ops.previous.bak
    ops_remove_tree "$DEPLOY/ops.new"
    if [[ $OPS_CREATED == 1 ]]; then ops_remove_tree "$target"; fi
    if [[ $OPS_MOVED == 1 ]]; then mv -- "$old" "$target"; fi
    if [[ $OPS_PARKED == 1 ]]; then mv -- "$parked" "$old"; fi
    OPS_UNDO=0
}

# The deployment succeeded: the copy before ops.previous is no longer needed.
complete_ops() {
    OPS_UNDO=0
    ops_remove_tree "$DEPLOY/ops.previous.bak"
}

# ops and ops.previous trade places (a rollback, or the undo of one). All or nothing: a move that fails undoes the moves
# before it, so a caller that sees a failure knows nothing was swapped.
swap_ops() {
    local current=$DEPLOY/ops other=$DEPLOY/ops.previous temporary=$DEPLOY/ops.swap parked=0
    [[ -e $other ]] || return 0
    ops_remove_tree "$temporary" || return 1
    if [[ -e $current ]]; then mv -- "$current" "$temporary" || return 1; parked=1; fi
    if ! mv -- "$other" "$current"; then
        if ((parked)); then mv -- "$temporary" "$current"; fi
        return 1
    fi
    if ((parked)) && ! mv -- "$temporary" "$other"; then
        mv -- "$current" "$other" && mv -- "$temporary" "$current"
        return 1
    fi
}
