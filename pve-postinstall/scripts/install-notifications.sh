#!/bin/bash
# install-notifications.sh - Install Proxmox notification config
# Usage: ./scripts/install-notifications.sh [hostname]

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
    print_error() { echo "    ✗ Error: $*" >&2; }
fi

if [[ ! -f "$BUILD_DIR/notifications.cfg" ]]; then
    print_error "Missing notifications.cfg at $BUILD_DIR/notifications.cfg"
    exit 1
fi

if [[ ! -f "$BUILD_DIR/priv-notifications.cfg" ]]; then
    print_error "Missing priv-notifications.cfg at $BUILD_DIR/priv-notifications.cfg"
    exit 1
fi

mkdir -p /etc/pve/priv

if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f /etc/pve/notifications.cfg ]] || ! cmp -s "$BUILD_DIR/notifications.cfg" /etc/pve/notifications.cfg; then
    backup_config /etc/pve/notifications.cfg
    print_sub "Installing notifications.cfg..."
    cp "$BUILD_DIR/notifications.cfg" /etc/pve/notifications.cfg
else
    print_sub "notifications.cfg unchanged; skipping update"
fi

if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f /etc/pve/priv/notifications.cfg ]] || ! cmp -s "$BUILD_DIR/priv-notifications.cfg" /etc/pve/priv/notifications.cfg; then
    backup_config /etc/pve/priv/notifications.cfg
    print_sub "Installing priv-notifications.cfg..."
    cp "$BUILD_DIR/priv-notifications.cfg" /etc/pve/priv/notifications.cfg
else
    print_sub "priv-notifications.cfg unchanged; skipping update"
fi

chown root:www-data /etc/pve/notifications.cfg /etc/pve/priv/notifications.cfg
chmod 640 /etc/pve/notifications.cfg
chmod 600 /etc/pve/priv/notifications.cfg

print_sub "PVE notifications deployed"
