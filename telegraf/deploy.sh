#!/bin/bash
# Deploy Telegraf Monitoring to Cluster
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="${SCRIPT_DIR}/configs"
COMMON_DIR="${CONFIGS_DIR}/common"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

# Get supported hosts from registry (PVE + PBS)
SUPPORTED_HOSTS=($(get_hosts_by_type pve) $(get_hosts_by_type pbs))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping telegraf (not applicable to $1)"
    exit 0
fi

# Validate common configs exist
if [[ ! -f "$COMMON_DIR/telegraf.conf" ]]; then
    echo "Error: telegraf.conf not found at $COMMON_DIR/telegraf.conf"
    exit 1
fi

print_action "Deploying Telegraf Monitoring"
print_sub "Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    print_action "Deploying to $host..."

    # Ensure InfluxData repository is configured
    if ! ssh "$host" "test -f /etc/apt/sources.list.d/influxdata.list"; then
        print_sub "Adding InfluxData repository..."
        ssh "$host" "mkdir -p /etc/apt/keyrings"
        ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
        ssh "$host" "echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list"
    else
        print_sub "InfluxData repository already configured"
    fi

    # Refresh repository key
    print_sub "Refreshing InfluxData repository key..."
    ssh "$host" "mkdir -p /etc/apt/keyrings"
    ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
    ssh "$host" "apt-get update -qq"

    # Install required packages
    print_sub "Installing packages (telegraf, lm-sensors, smartmontools)..."
    ssh "$host" "apt-get install -y -qq telegraf lm-sensors smartmontools"

    # Configure lm-sensors
    print_sub "Configuring lm-sensors..."
    ssh "$host" "sensors-detect --auto >/dev/null 2>&1 || true"

    # Ensure directories exist
    ssh "$host" "mkdir -p /etc/telegraf/telegraf.d"

    # Deploy main telegraf.conf
    print_sub "Deploying telegraf.conf..."
    deploy_file "$COMMON_DIR/telegraf.conf" "$host" "/etc/telegraf/telegraf.conf"

    # Deploy common configs to telegraf.d/
    print_sub "Deploying common configs..."
    for conf in sensors.conf smartctl.conf diskio.conf net.conf mem.conf; do
        if [[ -f "$COMMON_DIR/$conf" ]]; then
            deploy_file "$COMMON_DIR/$conf" "$host" "/etc/telegraf/telegraf.d/$conf"
        fi
    done

    # Deploy sudoers rule for smartctl
    if [[ -f "$COMMON_DIR/telegraf-smartctl-sudoers" ]]; then
        print_sub "Deploying sudoers rule..."
        deploy_file "$COMMON_DIR/telegraf-smartctl-sudoers" "$host" "/etc/sudoers.d/telegraf-smartctl" "440"
    fi

    # Deploy host-specific configs (overrides common)
    if [[ -d "$CONFIGS_DIR/$host" ]]; then
        print_sub "Deploying $host-specific configs..."
        for conf in "$CONFIGS_DIR/$host"/*.conf; do
            [[ -f "$conf" ]] || continue
            deploy_file "$conf" "$host" "/etc/telegraf/telegraf.d/$(basename "$conf")"
        done
    fi

    # Deploy bray-specific boot script
    if [[ "$host" == "bray" && -f "$SCRIPT_DIR/scripts/smartctl-bray-boot.py" ]]; then
        print_sub "Deploying bray boot smartctl script..."
        ssh "$host" "mkdir -p /usr/local/bin"
        deploy_script "$SCRIPT_DIR/scripts/smartctl-bray-boot.py" "$host" "/usr/local/bin/telegraf-smartctl-bray-boot"
    fi

    # Enable and restart telegraf
    print_sub "Enabling and restarting telegraf service..."
    ssh "$host" "systemctl enable telegraf && systemctl restart telegraf"

    # Check status
    if ssh "$host" "systemctl is-active --quiet telegraf"; then
        print_ok "Telegraf running on $host"
    else
        print_warn "Telegraf not running on $host"
        ssh "$host" "systemctl status telegraf --no-pager -l" || true
    fi

    echo ""
done

print_action "Deployment complete!"
