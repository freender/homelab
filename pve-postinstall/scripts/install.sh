#!/bin/bash
# install.sh - Install PVE/PBS post-install configs
# Usage: ./scripts/install.sh [hostname] [pve|pbs]

set -e

HOST=${1:-$(hostname)}
HOST_TYPE=${2:-}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    }
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
fi

if [[ -z "$HOST_TYPE" ]]; then
    if command -v pveversion >/dev/null 2>&1; then
        HOST_TYPE="pve"
    elif command -v proxmox-backup-manager >/dev/null 2>&1; then
        HOST_TYPE="pbs"
    fi
fi

if [[ -z "$HOST_TYPE" ]]; then
    echo "Error: host type not provided and could not be detected"
    exit 1
fi

if [[ ! -d "$BUILD_DIR" ]]; then
    echo "Error: Missing build directory $BUILD_DIR"
    exit 1
fi

print_sub "Backing up repo configs..."
backup_config /etc/apt/sources.list.d
backup_config /etc/apt/apt.conf.d/no-nag-script

case "$HOST_TYPE" in
    pve)
        for file in proxmox.sources pve-enterprise.sources ceph.sources pve-test.sources no-nag-script pve-remove-nag.sh; do
            if [[ ! -f "$BUILD_DIR/$file" ]]; then
                echo "Error: Missing $file in $BUILD_DIR"
                exit 1
            fi
        done

        print_sub "Deploying PVE repo sources..."
        cp "$BUILD_DIR/proxmox.sources" /etc/apt/sources.list.d/proxmox.sources
        cp "$BUILD_DIR/pve-enterprise.sources" /etc/apt/sources.list.d/pve-enterprise.sources
        cp "$BUILD_DIR/ceph.sources" /etc/apt/sources.list.d/ceph.sources
        cp "$BUILD_DIR/pve-test.sources" /etc/apt/sources.list.d/pve-test.sources

        print_sub "Deploying nag removal..."
        mkdir -p /usr/local/bin
        cp "$BUILD_DIR/pve-remove-nag.sh" /usr/local/bin/pve-remove-nag.sh
        chmod 755 /usr/local/bin/pve-remove-nag.sh
        cp "$BUILD_DIR/no-nag-script" /etc/apt/apt.conf.d/no-nag-script
        chmod 644 /etc/apt/apt.conf.d/no-nag-script
        ;;
    pbs)
        for file in proxmox.sources pbs-enterprise.sources no-nag-script; do
            if [[ ! -f "$BUILD_DIR/$file" ]]; then
                echo "Error: Missing $file in $BUILD_DIR"
                exit 1
            fi
        done

        print_sub "Deploying PBS repo sources..."
        cp "$BUILD_DIR/proxmox.sources" /etc/apt/sources.list.d/proxmox.sources
        cp "$BUILD_DIR/pbs-enterprise.sources" /etc/apt/sources.list.d/pbs-enterprise.sources

        print_sub "Deploying nag removal..."
        cp "$BUILD_DIR/no-nag-script" /etc/apt/apt.conf.d/no-nag-script
        chmod 644 /etc/apt/apt.conf.d/no-nag-script
        ;;
    *)
        print_warn "Unsupported host type: $HOST_TYPE"
        exit 1
        ;;
esac

print_sub "Refreshing proxmox widget toolkit..."
apt --reinstall install proxmox-widget-toolkit &>/dev/null || print_warn "Widget toolkit reinstall failed"

print_sub "Updating system packages..."
apt update &>/dev/null || print_warn "apt update failed"
apt -y dist-upgrade &>/dev/null || print_warn "apt dist-upgrade failed"
