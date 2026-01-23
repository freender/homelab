#!/bin/bash
# install.sh - Install telegraf on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

backup_config() {
    local path="$1"
    [[ -e "$path" ]] || return 0
    cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
}

if [[ ! -d "$BUILD_DIR" ]]; then
    echo "Error: Build directory not found: $BUILD_DIR"
    exit 1
fi

# Setup InfluxData repository
if [[ ! -f /etc/apt/sources.list.d/influxdata.list ]]; then
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://repos.influxdata.com/influxdata-archive.key | gpg --dearmor --yes --batch -o /etc/apt/keyrings/influxdata-archive.gpg
    echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list
fi

apt-get update -qq
apt-get install -y -qq telegraf lm-sensors smartmontools
sensors-detect --auto >/dev/null 2>&1 || true

mkdir -p /etc/telegraf/telegraf.d
backup_config /etc/telegraf/telegraf.conf
cp "$BUILD_DIR/telegraf.conf" /etc/telegraf/telegraf.conf
cp -r "$BUILD_DIR/telegraf.d"/* /etc/telegraf/telegraf.d/

if [[ -f "$BUILD_DIR/telegraf-smartctl-sudoers" ]]; then
    cp "$BUILD_DIR/telegraf-smartctl-sudoers" /etc/sudoers.d/telegraf-smartctl
    chmod 440 /etc/sudoers.d/telegraf-smartctl
fi

systemctl enable --now telegraf
systemctl is-active --quiet telegraf
