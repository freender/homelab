#!/bin/bash

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1

load_file_map

print_header "Media Pool"

print_action "MergerFS"
if ! command -v mergerfs >/dev/null 2>&1; then
    apt-get install -y -q mergerfs
    print_ok "mergerfs installed"
else
    print_sub "mergerfs already installed"
fi

primary_units_changed=false
hdd_units_changed=false
rc=0
install_build_file "homelab-media-pool.service" || rc=$?
[[ $rc -eq 0 ]] && primary_units_changed=true

if [[ -f "$BUILD_DIR/homelab-media-pool-hdd-only.service" ]]; then
    rc=0
    install_build_file "homelab-media-pool-hdd-only.service" || rc=$?
    [[ $rc -eq 0 ]] && hdd_units_changed=true
fi

for retired_unit in \
    homelab-tiered-media.service \
    homelab-tiered-media-hdd.service; do
    if systemctl is-enabled --quiet "$retired_unit" 2>/dev/null; then
        systemctl disable --now "$retired_unit"
        print_ok "$retired_unit disabled"
    elif systemctl is-active --quiet "$retired_unit" 2>/dev/null; then
        systemctl stop "$retired_unit"
        print_ok "$retired_unit stopped"
    fi
    if [[ -f "/etc/systemd/system/$retired_unit" ]]; then
        rm -f "/etc/systemd/system/$retired_unit"
        print_ok "$retired_unit removed"
        primary_units_changed=true
    fi
done

if [[ "$primary_units_changed" == "true" || "$hdd_units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

if ! systemctl is-enabled --quiet homelab-media-pool.service 2>/dev/null; then
    systemctl enable --now homelab-media-pool.service
    print_ok "homelab-media-pool.service enabled"
elif [[ "$primary_units_changed" == "true" ]]; then
    systemctl restart homelab-media-pool.service
    print_ok "homelab-media-pool.service restarted"
elif ! systemctl is-active --quiet homelab-media-pool.service 2>/dev/null; then
    systemctl start homelab-media-pool.service
    print_ok "homelab-media-pool.service started"
else
    print_sub "homelab-media-pool.service already enabled"
fi

if [[ -f "$BUILD_DIR/homelab-media-pool-hdd-only.service" ]]; then
    if ! systemctl is-enabled --quiet homelab-media-pool-hdd-only.service 2>/dev/null; then
        systemctl enable --now homelab-media-pool-hdd-only.service
        print_ok "homelab-media-pool-hdd-only.service enabled"
    elif [[ "$hdd_units_changed" == "true" ]]; then
        systemctl restart homelab-media-pool-hdd-only.service
        print_ok "homelab-media-pool-hdd-only.service restarted"
    elif ! systemctl is-active --quiet homelab-media-pool-hdd-only.service 2>/dev/null; then
        systemctl start homelab-media-pool-hdd-only.service
        print_ok "homelab-media-pool-hdd-only.service started"
    else
        print_sub "homelab-media-pool-hdd-only.service already enabled"
    fi
fi

print_header "Media Pool Complete"
