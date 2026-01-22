#!/bin/bash
# Deploy Telegraf Monitoring
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
CONFIGS_DIR="${SCRIPT_DIR}/configs"
COMMON_DIR="${CONFIGS_DIR}/common"
APC_CONFIG="${CONFIGS_DIR}/roles/apc/apcupsd.conf"

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
    
    # Check APC config if any host needs it
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
    
    # Setup InfluxData repository
    if ! ssh "$host" "test -f /etc/apt/sources.list.d/influxdata.list"; then
        print_sub "Adding InfluxData repository..."
        ssh "$host" "mkdir -p /etc/apt/keyrings"
        ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
        ssh "$host" "echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list"
    fi
    
    print_sub "Installing packages..."
    ssh "$host" "apt-get update -qq && apt-get install -y -qq telegraf lm-sensors smartmontools"
    ssh "$host" "sensors-detect --auto >/dev/null 2>&1 || true"
    ssh "$host" "mkdir -p /etc/telegraf/telegraf.d"
    
    print_sub "Deploying configs..."
    deploy_file "$COMMON_DIR/telegraf.conf" "$host" "/etc/telegraf/telegraf.conf"
    for conf in sensors.conf smartctl.conf diskio.conf net.conf mem.conf; do
        deploy_file "$COMMON_DIR/$conf" "$host" "/etc/telegraf/telegraf.d/$conf"
    done
    
    if hosts has "$host" "telegraf-apc"; then
        print_sub "Deploying apcupsd input config..."
        deploy_file "$APC_CONFIG" "$host" "/etc/telegraf/telegraf.d/apcupsd.conf"
    fi
    
    if [[ -f "$COMMON_DIR/telegraf-smartctl-sudoers" ]]; then
        deploy_file "$COMMON_DIR/telegraf-smartctl-sudoers" "$host" "/etc/sudoers.d/telegraf-smartctl" "440"
    fi
    
    if [[ -d "$CONFIGS_DIR/$host" ]]; then
        print_sub "Deploying $host-specific configs..."
        for conf in "$CONFIGS_DIR/$host"/*.conf; do
            [[ -f "$conf" ]] || continue
            deploy_file "$conf" "$host" "/etc/telegraf/telegraf.d/$(basename "$conf")"
        done
    fi
    
    enable_service "$host" "telegraf"
    verify_service "$host" "telegraf"
}

# --- Main ---
deploy_init "Telegraf Monitoring"
deploy_run deploy $HOSTS
deploy_finish
