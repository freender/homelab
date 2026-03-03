#!/bin/bash
# install.sh - Install GPU passthrough configs
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
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    }
    print_sub() { echo "    $*"; }
fi

if [[ ! -f "$BUILD_DIR/blacklist.conf" || ! -f "$BUILD_DIR/cmdline" || ! -f "$BUILD_DIR/vfio.conf" || ! -f "$BUILD_DIR/modules" ]]; then
    echo "Error: Missing build artifacts in $BUILD_DIR"
    exit 1
fi

if [[ ! -f /etc/kernel/cmdline ]]; then
    echo "Error: /etc/kernel/cmdline not found (systemd-boot required)"
    exit 1
fi

print_sub "Backing up configs..."
backup_config /etc/kernel/cmdline
backup_config /etc/modules

initramfs_needs_update=false
boot_refresh_needed=false

print_sub "Updating systemd-boot cmdline..."
cmdline=$(head -n 1 "$BUILD_DIR/cmdline")
current_cmdline=""
if [[ -f /etc/kernel/cmdline ]]; then
    current_cmdline=$(head -n 1 /etc/kernel/cmdline)
fi
if [[ "$current_cmdline" != "$cmdline" ]]; then
    printf '%s\n' "$cmdline" > /etc/kernel/cmdline
    boot_refresh_needed=true
fi

print_sub "Deploying modprobe configs..."
if [[ ! -f /etc/modprobe.d/blacklist.conf ]] || ! cmp -s "$BUILD_DIR/blacklist.conf" /etc/modprobe.d/blacklist.conf; then
    cp "$BUILD_DIR/blacklist.conf" /etc/modprobe.d/blacklist.conf
    initramfs_needs_update=true
fi
if [[ ! -f /etc/modprobe.d/vfio.conf ]] || ! cmp -s "$BUILD_DIR/vfio.conf" /etc/modprobe.d/vfio.conf; then
    cp "$BUILD_DIR/vfio.conf" /etc/modprobe.d/vfio.conf
    initramfs_needs_update=true
fi

print_sub "Deploying VFIO modules..."
if [[ ! -f /etc/modules-load.d/vfio.conf ]] || ! cmp -s "$BUILD_DIR/modules" /etc/modules-load.d/vfio.conf; then
    cp "$BUILD_DIR/modules" /etc/modules-load.d/vfio.conf
    initramfs_needs_update=true
fi

print_sub "Cleaning legacy /etc/modules..."
if [[ -f /etc/modules ]]; then
    grep -v '^vfio' /etc/modules > /tmp/modules.clean
    if ! cmp -s /tmp/modules.clean /etc/modules; then
        mv /tmp/modules.clean /etc/modules
        initramfs_needs_update=true
    else
        rm -f /tmp/modules.clean
    fi
fi

print_sub "Deploying emergency removal script..."
if [[ -f "$SCRIPT_DIR/scripts/remove-local.sh" ]]; then
    cp "$SCRIPT_DIR/scripts/remove-local.sh" /root/pve-gpu-passthrough-remove.sh
    chmod +x /root/pve-gpu-passthrough-remove.sh
fi

print_sub "Updating initramfs..."
if [[ "$initramfs_needs_update" == "true" ]]; then
    update-initramfs -u -k all
else
    print_sub "No module changes detected; skipping initramfs update"
fi

if [[ "$boot_refresh_needed" == "true" ]]; then
    print_sub "Refreshing systemd-boot..."
    proxmox-boot-tool refresh
else
    print_sub "Kernel cmdline unchanged; skipping systemd-boot refresh"
fi
