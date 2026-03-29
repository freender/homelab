#!/bin/bash
# install.sh - Install GPU passthrough configs
# Usage: ./scripts/install.sh [hostname]

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

require_file "$BUILD_DIR/blacklist.conf" "$BUILD_DIR/blacklist.conf" || exit 1
require_file "$BUILD_DIR/cmdline" "$BUILD_DIR/cmdline" || exit 1
require_file "$BUILD_DIR/vfio.conf" "$BUILD_DIR/vfio.conf" || exit 1
require_file "$BUILD_DIR/modules" "$BUILD_DIR/modules" || exit 1

if [[ ! -f /etc/kernel/cmdline ]]; then
    echo "Error: /etc/kernel/cmdline not found (systemd-boot required)"
    exit 1
fi

print_sub "Backing up configs..."
backup_config /etc/kernel/cmdline
backup_config /etc/modules

initramfs_needs_update=false
boot_refresh_needed=false
required_root_token="root=ZFS=rpool/ROOT/pve-1"
required_root_dataset="${required_root_token#root=ZFS=}"

print_sub "Updating systemd-boot cmdline..."
cmdline=$(head -n 1 "$BUILD_DIR/cmdline")
if [[ "$cmdline" != *"$required_root_token"* ]]; then
    echo "Error: Refusing to write /etc/kernel/cmdline without required token: $required_root_token" >&2
    exit 1
fi
if ! zfs list -H -o name "$required_root_dataset" >/dev/null 2>&1; then
    echo "Error: Required ZFS dataset not found: $required_root_dataset" >&2
    exit 1
fi

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
