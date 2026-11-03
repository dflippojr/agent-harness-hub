#!/usr/bin/env bash
# The deployment check, read-only: the Hub's systemd user unit is enabled, active and ordered on no other service (an
# enabled unit that is not running, or whose start job systemd dropped at boot, fails); /healthz answers ok with the
# version in deployed.json; status.json is fresh and names the deployed commit. Exits 1 if any check fails.
# deploy-hub.sh ends every deployment and rollback with the same check.
#
# Usage: check-hub.sh [--deploy-dir DIR]   (default: the deployment it runs from, else ~/.local/share/agent-harness-hub)
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=hub-common.sh
source "$here/hub-common.sh"

deploy_dir=''
while (($#)); do
    case $1 in
        --deploy-dir) deploy_dir=${2:?--deploy-dir needs a value}; shift 2 ;;
        -h | --help) sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) hub_die "unknown argument: $1 (see --help)" ;;
    esac
done
hub_set_paths "${deploy_dir:-$(hub_own_deploy_dir "$here")}"
hub_reload_env
hub_check
