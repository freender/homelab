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

print_header "Installing SnapRAID"

# 1. Install snapraid package
if ! command -v snapraid >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q snapraid
    print_ok "snapraid installed"
else
    print_sub "snapraid already installed"
fi

units_changed=false
timer_changed=false

for unit in \
    snapraid.conf \
    homelab-snapraid-sync.service \
    homelab-snapraid-sync.timer \
    homelab-snapraid-scrub.service \
    homelab-snapraid-scrub.timer \
    homelab-snapraid-progress-log \
    homelab-snapraid-failure-notify \
    homelab-snapraid-status-notify; do
    rc=0
    install_build_file "$unit" || rc=$?
    if [[ $rc -eq 0 ]]; then
        if [[ "$unit" == "homelab-snapraid-sync.timer" || "$unit" == "homelab-snapraid-scrub.timer" ]]; then
            timer_changed=true
        fi
        units_changed=true
    fi
done

for retired_unit in \
    snapraid-sync.service \
    snapraid-sync.timer \
    snapraid-scrub.service \
    snapraid-scrub.timer; do
    systemctl unmask "$retired_unit" >/dev/null 2>&1 || true
    rm -f "/run/systemd/system/$retired_unit"
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
        units_changed=true
    fi
done

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

ensure_timer_state homelab-snapraid-sync.timer true "$timer_changed"
ensure_timer_state homelab-snapraid-scrub.timer true "$timer_changed"
