#!/usr/bin/env bash
# Stops the Hub and removes its systemd user unit, so it no longer starts at boot. Keeps <deploy-dir> (environments,
# records, hub.env and state/, which holds the Hub key); delete that directory yourself to remove everything. The
# daemon's own Hub record remains until `harness hub release --confirm` on the daemon host.
#
# Usage: uninstall-unit.sh [--deploy-dir DIR]
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=hub-common.sh
source "$here/hub-common.sh"

deploy_dir=''
while (($#)); do
    case $1 in
        --deploy-dir) deploy_dir=${2:?--deploy-dir needs a value}; shift 2 ;;
        -h | --help) sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) hub_die "unknown argument: $1 (see --help)" ;;
    esac
done
hub_set_paths "${deploy_dir:-$(hub_own_deploy_dir "$here")}"
hub_stop || hub_die "stopping the Hub reported an error, so the unit is left installed; the Hub may be stopped or" \
    "running (systemctl --user status $HUB_UNIT)"
if hub_unit_installed; then
    systemctl --user disable "$HUB_UNIT"
    rm -f -- "$UNIT_FILE"
    systemctl --user daemon-reload
fi
hub_say "$HUB_UNIT removed; $DEPLOY is kept."
