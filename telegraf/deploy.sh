#!/bin/bash
# Deploy Telegraf Monitoring to Cluster
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

set -e

HOSTS="${@:-ace bray clovis xur}"
if [[ "$1" == "all" ]]; then
    HOSTS="ace bray clovis xur"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_CONFIG="${SCRIPT_DIR}/telegraf.conf"
SENSORS_CONFIG="${SCRIPT_DIR}/sensors.conf"
SMARTCTL_CONFIG="${SCRIPT_DIR}/smartctl.conf"
DISKIO_CONFIG="${SCRIPT_DIR}/diskio.conf"
NET_CONFIG="${SCRIPT_DIR}/net.conf"
MEM_CONFIG="${SCRIPT_DIR}/mem.conf"
BRAY_SMARTCTL_CONFIG="${SCRIPT_DIR}/smartctl-bray.conf"
BRAY_SMARTCTL_SCRIPT="${SCRIPT_DIR}/smartctl-bray-boot.py"
BRAY_SMARTCTL_INPUT_CONFIG="${SCRIPT_DIR}/smartctl-bray-smartctl.conf"
APCUPSD_CONFIG="${SCRIPT_DIR}/apcupsd.conf"

if [[ ! -f "$MAIN_CONFIG" ]]; then
    echo "Error: telegraf.conf not found at $MAIN_CONFIG"
    exit 1
fi

echo "==> Deploying Telegraf Monitoring"
echo "    Hosts: $HOSTS"
echo "    Collection Interval: 10s (sensors), 10s (smartctl), 10s (diskio)"
echo ""

for host in $HOSTS; do
    echo "==> Deploying to $host..."

    # Ensure InfluxData repository is configured and key is current
    if ! ssh "$host" "test -f /etc/apt/sources.list.d/influxdata.list"; then
        echo "    Adding InfluxData repository..."
        ssh "$host" "mkdir -p /etc/apt/keyrings"
        ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
        ssh "$host" "echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list"
    else
        echo "    InfluxData repository already configured"
    fi

    echo "    Refreshing InfluxData repository key..."
    ssh "$host" "mkdir -p /etc/apt/keyrings"
    ssh "$host" "curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg"
    echo "    Testing apt update after key refresh..."
    ssh "$host" "apt-get update -qq"

    # Install required packages
    echo "    Installing packages (telegraf, lm-sensors, smartmontools)..."
    ssh "$host" "apt-get update -qq && apt-get install -y telegraf lm-sensors smartmontools"

    # Ensure lm-sensors is configured
    echo "    Configuring lm-sensors..."
    ssh "$host" "sensors-detect --auto >/dev/null 2>&1 || true"

    # Deploy main configuration
    echo "    Deploying telegraf.conf..."
    scp "$MAIN_CONFIG" "${host}:/etc/telegraf/telegraf.conf"
    ssh "$host" "chown root:root /etc/telegraf/telegraf.conf && chmod 644 /etc/telegraf/telegraf.conf"

    # Ensure telegraf.d directory exists
    ssh "$host" "mkdir -p /etc/telegraf/telegraf.d"

    # Deploy sensors configuration
    echo "    Deploying sensors.conf..."
    scp "$SENSORS_CONFIG" "${host}:/etc/telegraf/telegraf.d/sensors.conf"
    ssh "$host" "chown root:root /etc/telegraf/telegraf.d/sensors.conf && chmod 644 /etc/telegraf/telegraf.d/sensors.conf"

    # Deploy smartctl configuration (inputs.smartctl)
    if [[ "$host" == "bray" && -f "$BRAY_SMARTCTL_INPUT_CONFIG" ]]; then
        echo "    Deploying bray smartctl.conf override..."
        scp "$BRAY_SMARTCTL_INPUT_CONFIG" "${host}:/etc/telegraf/telegraf.d/smartctl.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/smartctl.conf && chmod 644 /etc/telegraf/telegraf.d/smartctl.conf"
    elif [[ -f "$SMARTCTL_CONFIG" ]]; then
        echo "    Deploying smartctl.conf..."
        scp "$SMARTCTL_CONFIG" "${host}:/etc/telegraf/telegraf.d/smartctl.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/smartctl.conf && chmod 644 /etc/telegraf/telegraf.d/smartctl.conf"
    fi

    # Deploy sudoers rule for smartctl
    if [[ -f "${SCRIPT_DIR}/telegraf-smartctl-sudoers" ]]; then
        echo "    Deploying sudoers rule for smartctl..."
        scp "${SCRIPT_DIR}/telegraf-smartctl-sudoers" "${host}:/etc/sudoers.d/telegraf-smartctl"
        ssh "$host" "chown root:root /etc/sudoers.d/telegraf-smartctl && chmod 440 /etc/sudoers.d/telegraf-smartctl"
    fi

    # Deploy bray boot smartctl exec config
    if [[ "$host" == "bray" && -f "$BRAY_SMARTCTL_CONFIG" && -f "$BRAY_SMARTCTL_SCRIPT" ]]; then
        echo "    Deploying bray boot smartctl exec config..."
        scp "$BRAY_SMARTCTL_CONFIG" "${host}:/etc/telegraf/telegraf.d/smartctl-bray.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/smartctl-bray.conf && chmod 644 /etc/telegraf/telegraf.d/smartctl-bray.conf"

        echo "    Deploying bray boot smartctl exec script..."
        ssh "$host" "mkdir -p /usr/local/bin"
        scp "$BRAY_SMARTCTL_SCRIPT" "${host}:/usr/local/bin/telegraf-smartctl-bray-boot"
        ssh "$host" "chown root:root /usr/local/bin/telegraf-smartctl-bray-boot && chmod 755 /usr/local/bin/telegraf-smartctl-bray-boot"
    fi

    # Deploy diskio configuration
    if [[ -f "$DISKIO_CONFIG" ]]; then
        echo "    Deploying diskio.conf..."
        scp "$DISKIO_CONFIG" "${host}:/etc/telegraf/telegraf.d/diskio.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/diskio.conf && chmod 644 /etc/telegraf/telegraf.d/diskio.conf"
    fi

    # Deploy net configuration
    if [[ -f "$NET_CONFIG" ]]; then
        echo "    Deploying net.conf..."
        scp "$NET_CONFIG" "${host}:/etc/telegraf/telegraf.d/net.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/net.conf && chmod 644 /etc/telegraf/telegraf.d/net.conf"
    fi

    # Deploy mem configuration
    if [[ -f "$MEM_CONFIG" ]]; then
        echo "    Deploying mem.conf..."
        scp "$MEM_CONFIG" "${host}:/etc/telegraf/telegraf.d/mem.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/mem.conf && chmod 644 /etc/telegraf/telegraf.d/mem.conf"
    fi


    # Deploy apcupsd configuration (only to hosts with UPS)
    if [[ "$host" == "bray" || "$host" == "xur" ]] && [[ -f "$APCUPSD_CONFIG" ]]; then
        echo "    Deploying apcupsd.conf..."
        scp "$APCUPSD_CONFIG" "${host}:/etc/telegraf/telegraf.d/apcupsd.conf"
        ssh "$host" "chown root:root /etc/telegraf/telegraf.d/apcupsd.conf && chmod 644 /etc/telegraf/telegraf.d/apcupsd.conf"
    fi

    # Enable and restart telegraf
    echo "    Enabling and restarting telegraf service..."
    ssh "$host" "systemctl enable telegraf && systemctl restart telegraf"

    # Check status
    echo "    Checking service status..."
    if ssh "$host" "systemctl is-active --quiet telegraf"; then
        echo "    ✓ Telegraf running on $host"
    else
        echo "    ✗ Warning: Telegraf not running on $host"
        ssh "$host" "systemctl status telegraf --no-pager -l" || true
    fi

    echo ""
done

echo "==> Deployment complete!"
