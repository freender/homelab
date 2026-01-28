#!/bin/bash
# Deploy Telegraf Monitoring
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$SCRIPT_DIR/configs"
COMMON_DIR="$CONFIGS_DIR/common"
APC_CONFIG="$CONFIGS_DIR/roles/apc/apcupsd.conf"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature telegraf)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping telegraf (not applicable to $1)"
    exit 0
fi

# --- Validation ---
validate() {
    local required=(telegraf.conf sensors.conf smartctl.conf diskio.conf disk.conf net.conf mem.conf)
    [[ ! -d "$COMMON_DIR" ]] && { echo "Error: $COMMON_DIR not found"; return 1; }
    for conf in "${required[@]}"; do
        [[ ! -f "$COMMON_DIR/$conf" ]] && { echo "Error: Missing $COMMON_DIR/$conf"; return 1; }
    done

    for host in $HOSTS; do
        local ups_role
        ups_role=$(hosts get "$host" "apcupsd.role" "none")
        if [[ "$ups_role" == "master" || "$ups_role" == "master-standalone" ]]; then

            if [[ ! -f "$APC_CONFIG" ]]; then
                echo "Error: APC config not found for master node $host: $APC_CONFIG"
                return 1
            fi
        fi
    done
}
validate || exit 1

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"
    local ups_role

    prepare_build_dir "$build_dir"
    mkdir -p "$build_dir/telegraf.d"

    cp "$COMMON_DIR/telegraf.conf" "$build_dir/telegraf.conf"
    for conf in sensors.conf smartctl.conf diskio.conf disk.conf net.conf mem.conf; do
        cp "$COMMON_DIR/$conf" "$build_dir/telegraf.d/$conf"
    done

    ups_role=$(hosts get "$host" "apcupsd.role" "none")
    if [[ "$ups_role" == "master" || "$ups_role" == "master-standalone" ]]; then
        cp "$APC_CONFIG" "$build_dir/telegraf.d/apcupsd.conf"
    fi

    if hosts has "$host" "zfs"; then
        cp "$CONFIGS_DIR/roles/zfs/zfs.conf" "$build_dir/telegraf.d/zfs.conf"
    fi

    if [[ -f "$COMMON_DIR/telegraf-smartctl-sudoers" ]]; then
        cp "$COMMON_DIR/telegraf-smartctl-sudoers" "$build_dir/telegraf-smartctl-sudoers"
    fi

    if [[ -d "$CONFIGS_DIR/$host" ]]; then
        for conf in "$CONFIGS_DIR/$host"/*.conf; do
            [[ -f "$conf" ]] || continue
            cp "$conf" "$build_dir/telegraf.d/$(basename "$conf")"
        done
    fi

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-telegraf/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-telegraf && mkdir -p /tmp/homelab-telegraf/build /tmp/homelab-telegraf/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-telegraf/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-telegraf/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-telegraf/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-telegraf && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "Telegraf Monitoring"
deploy_run deploy $HOSTS
deploy_finish
