#!/bin/bash
# install.sh - Install Proxmox notification config
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

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

if [[ ! -f "$BUILD_DIR/notifications.cfg" ]]; then
    echo "Error: Missing notifications.cfg at $BUILD_DIR/notifications.cfg"
    exit 1
fi

if [[ ! -f "$BUILD_DIR/priv-notifications.cfg" ]]; then
    echo "Error: Missing priv-notifications.cfg at $BUILD_DIR/priv-notifications.cfg"
    exit 1
fi

print_sub "Backing up notification config..."
backup_config /etc/pve/notifications.cfg
backup_config /etc/pve/priv/notifications.cfg

print_sub "Installing notifications config..."
mkdir -p /etc/pve/priv
cp "$BUILD_DIR/notifications.cfg" /etc/pve/notifications.cfg
cp "$BUILD_DIR/priv-notifications.cfg" /etc/pve/priv/notifications.cfg

chown root:www-data /etc/pve/notifications.cfg /etc/pve/priv/notifications.cfg
chmod 640 /etc/pve/notifications.cfg
chmod 600 /etc/pve/priv/notifications.cfg

print_sub "PVE notifications deployed"
