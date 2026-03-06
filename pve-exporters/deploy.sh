#!/bin/bash
# Deploy Prometheus exporters to PVE hosts
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$SCRIPT_DIR/configs"
COMMON_DIR="$CONFIGS_DIR/common"
BUILD_ROOT="$SCRIPT_DIR/build"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-exporters)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-exporters (not applicable to $1)"
    exit 0
fi

validate() {
    local required=(node-exporter.defaults smartctl-exporter.defaults smartctl-exporter.service)
    [[ ! -d "$COMMON_DIR" ]] && { echo "Error: $COMMON_DIR not found"; return 1; }
    for conf in "${required[@]}"; do
        [[ ! -f "$COMMON_DIR/$conf" ]] && { echo "Error: Missing $COMMON_DIR/$conf"; return 1; }
    done
    return 0
}
validate || exit 1

deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"

    prepare_build_dir "$build_dir"
    mkdir -p "$build_dir/configs"

    cp "$COMMON_DIR/node-exporter.defaults" "$build_dir/configs/node-exporter.defaults"
    cp "$COMMON_DIR/smartctl-exporter.defaults" "$build_dir/configs/smartctl-exporter.defaults"
    cp "$COMMON_DIR/smartctl-exporter.service" "$build_dir/configs/smartctl-exporter.service"

    diff_remote_config "$host" "$build_dir/configs/node-exporter.defaults" "/etc/default/prometheus-node-exporter" || true
    diff_remote_config "$host" "$build_dir/configs/smartctl-exporter.defaults" "/etc/default/smartctl-exporter" || true
    diff_remote_config "$host" "$build_dir/configs/smartctl-exporter.service" "/etc/systemd/system/smartctl-exporter.service" || true

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-exporters/"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-exporters && mkdir -p /tmp/homelab-pve-exporters/build /tmp/homelab-pve-exporters/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-exporters/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-exporters/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-exporters/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-exporters && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo Error: PVE deploy requires root SSH user >&2; exit 1; fi && FORCE_UPDATE= ./scripts/install.sh "
}

deploy_init "PVE Prometheus Exporters"
deploy_run deploy $HOSTS
deploy_finish
