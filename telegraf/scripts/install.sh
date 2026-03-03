#!/bin/bash
# install.sh - Install telegraf on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub() { echo "    $*"; }
fi

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

if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f /etc/telegraf/telegraf.conf ]] || ! cmp -s "$BUILD_DIR/telegraf.conf" /etc/telegraf/telegraf.conf; then
    backup_config /etc/telegraf/telegraf.conf
    cp "$BUILD_DIR/telegraf.conf" /etc/telegraf/telegraf.conf
    print_sub "Updated /etc/telegraf/telegraf.conf"
else
    print_sub "telegraf.conf unchanged; skipping update"
fi

telegraf_d_updated=false
shopt -s nullglob
for source_file in "$BUILD_DIR/telegraf.d"/*; do
    target_file="/etc/telegraf/telegraf.d/$(basename "$source_file")"
    if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f "$target_file" ]] || ! cmp -s "$source_file" "$target_file"; then
        cp "$source_file" "$target_file"
        telegraf_d_updated=true
    fi
done
shopt -u nullglob

if [[ "$telegraf_d_updated" == "true" ]]; then
    print_sub "Updated telegraf.d snippets"
else
    print_sub "telegraf.d snippets unchanged; skipping update"
fi

if [[ -f "$BUILD_DIR/telegraf-smartctl-sudoers" ]]; then
    if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f /etc/sudoers.d/telegraf-smartctl ]] || ! cmp -s "$BUILD_DIR/telegraf-smartctl-sudoers" /etc/sudoers.d/telegraf-smartctl; then
        cp "$BUILD_DIR/telegraf-smartctl-sudoers" /etc/sudoers.d/telegraf-smartctl
        print_sub "Updated smartctl sudoers rule"
    else
        print_sub "smartctl sudoers rule unchanged; skipping update"
    fi
    chmod 440 /etc/sudoers.d/telegraf-smartctl
fi

systemctl enable --now telegraf
systemctl is-active --quiet telegraf
