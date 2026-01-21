#!/bin/bash
# Deploy Telegraf Monitoring
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="${SCRIPT_DIR}/configs"
COMMON_DIR="${CONFIGS_DIR}/common"
ROLE_DIR="${CONFIGS_DIR}/roles"
APC_ROLE_DIR="${ROLE_DIR}/apc"
APC_CONFIG="${APC_ROLE_DIR}/apcupsd.conf"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

get_telegraf_hosts() {
    get_hosts_with_feature telegraf
}

# Get supported hosts from registry
SUPPORTED_HOSTS=($(get_telegraf_hosts))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping telegraf (not applicable to $1)"
    exit 0
fi

validate_common_configs() {
    local required=(
        "telegraf.conf"
        "sensors.conf"
        "smartctl.conf"
        "diskio.conf"
        "net.conf"
        "mem.conf"
    )

    if [[ ! -d "$COMMON_DIR" ]]; then
        echo "Error: Telegraf common configs not found at $COMMON_DIR"
        return 1
    fi

    for conf in "${required[@]}"; do
        if [[ ! -f "$COMMON_DIR/$conf" ]]; then
            echo "Error: Missing telegraf config $COMMON_DIR/$conf"
            return 1
        fi
    done
}

if ! validate_common_configs; then
    exit 1
fi

needs_apc_config() {
    local host

    for host in $HOSTS; do
        if host_has_feature "$host" "telegraf-apc"; then
            return 0
        fi
    done

    return 1
}

if needs_apc_config && [[ ! -f "$APC_CONFIG" ]]; then
    echo "Error: APC role config not found at $APC_CONFIG"
    exit 1
fi

print_action "Deploying Telegraf Monitoring"
print_sub "Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    print_action "Deploying to $host..."

    if ! ssh "$host" "test -f /etc/apt/sources.list.d/influxdata.list"; then
        print_sub "Adding InfluxData repository..."
        ssh "$host" "mkdir -p /etc/apt/keyrings"
        ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
        ssh "$host" "echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list"
    else
        print_sub "InfluxData repository already configured"
    fi

    print_sub "Refreshing InfluxData repository key..."
    ssh "$host" "mkdir -p /etc/apt/keyrings"
    ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
    ssh "$host" "apt-get update -qq"

    print_sub "Installing packages (telegraf, lm-sensors, smartmontools)..."
    ssh "$host" "apt-get install -y -qq telegraf lm-sensors smartmontools"

    print_sub "Configuring lm-sensors..."
    ssh "$host" "sensors-detect --auto >/dev/null 2>&1 || true"

    ssh "$host" "mkdir -p /etc/telegraf/telegraf.d"

    print_sub "Deploying telegraf.conf..."
    deploy_file "$COMMON_DIR/telegraf.conf" "$host" "/etc/telegraf/telegraf.conf"

    print_sub "Deploying common configs..."
    for conf in sensors.conf smartctl.conf diskio.conf net.conf mem.conf; do
        if [[ -f "$COMMON_DIR/$conf" ]]; then
            deploy_file "$COMMON_DIR/$conf" "$host" "/etc/telegraf/telegraf.d/$conf"
        fi
    done

    if host_has_feature "$host" "telegraf-apc"; then
        print_sub "Deploying apcupsd input config..."
        deploy_file "$APC_CONFIG" "$host" "/etc/telegraf/telegraf.d/apcupsd.conf"
    fi

    if [[ -f "$COMMON_DIR/telegraf-smartctl-sudoers" ]]; then
        print_sub "Deploying sudoers rule..."
        deploy_file "$COMMON_DIR/telegraf-smartctl-sudoers" "$host" "/etc/sudoers.d/telegraf-smartctl" "440"
    fi

    if [[ -d "$CONFIGS_DIR/$host" ]]; then
        print_sub "Deploying $host-specific configs..."
        for conf in "$CONFIGS_DIR/$host"/*.conf; do
            [[ -f "$conf" ]] || continue
            deploy_file "$conf" "$host" "/etc/telegraf/telegraf.d/$(basename "$conf")"
        done
    fi

    print_sub "Enabling and restarting telegraf service..."
    ssh "$host" "systemctl enable telegraf && systemctl restart telegraf"

    if ssh "$host" "systemctl is-active --quiet telegraf"; then
        print_ok "Telegraf running on $host"
    else
        print_warn "Telegraf not running on $host"
        ssh "$host" "systemctl status telegraf --no-pager -l" || true
    fi

    echo ""
done

print_action "Deployment complete!"
