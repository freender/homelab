#!/bin/bash
# Deploy Telegraf Monitoring
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
CONFIGS_DIR="$SCRIPT_DIR/configs"
COMMON_DIR="$CONFIGS_DIR/common"
APC_CONFIG="$CONFIGS_DIR/roles/apc/apcupsd.conf"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
SUPPORTED_HOSTS=($(hosts list --feature telegraf))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping telegraf (not applicable to $1)"
    exit 0
fi

# --- Validation ---
validate() {
    local required=(telegraf.conf sensors.conf smartctl.conf diskio.conf net.conf mem.conf)
    [[ ! -d "$COMMON_DIR" ]] && { echo "Error: $COMMON_DIR not found"; return 1; }
    for conf in "${required[@]}"; do
        [[ ! -f "$COMMON_DIR/$conf" ]] && { echo "Error: Missing $COMMON_DIR/$conf"; return 1; }
    done

    for host in $HOSTS; do
        if hosts has "$host" "telegraf-apc" && [[ ! -f "$APC_CONFIG" ]]; then
            echo "Error: APC config not found: $APC_CONFIG"
            return 1
        fi
    done
}
validate || exit 1

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"

    rm -rf "$build_dir"
    mkdir -p "$build_dir/telegraf.d"

    cp "$COMMON_DIR/telegraf.conf" "$build_dir/telegraf.conf"
    for conf in sensors.conf smartctl.conf diskio.conf net.conf mem.conf; do
        cp "$COMMON_DIR/$conf" "$build_dir/telegraf.d/$conf"
    done

    if hosts has "$host" "telegraf-apc"; then
        cp "$APC_CONFIG" "$build_dir/telegraf.d/apcupsd.conf"
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

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-telegraf && mkdir -p /tmp/homelab-telegraf/build"
    scp -rq "$build_dir" "$host:/tmp/homelab-telegraf/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-telegraf/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-telegraf && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "Telegraf Monitoring"
deploy_run deploy $HOSTS
deploy_finish
