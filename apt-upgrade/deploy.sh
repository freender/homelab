#!/bin/bash
# Deploy apt dist-upgrade workflow
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature apt-upgrade)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apt-upgrade (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    case "$host_type" in
        pve|ubuntu)
            ;;
        *)
            print_warn "Skipping $host: apt-upgrade supports type pve/ubuntu only"
            return 0
            ;;
    esac

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would run apt dist-upgrade on $host"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-apt-upgrade && mkdir -p /tmp/homelab-apt-upgrade/lib /tmp/homelab-apt-upgrade/scripts"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-apt-upgrade/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-apt-upgrade/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-apt-upgrade && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh"
}

deploy_init "APT Dist-Upgrade"
deploy_run deploy $HOSTS
deploy_finish
