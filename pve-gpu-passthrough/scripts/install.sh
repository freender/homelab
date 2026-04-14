#!/bin/bash
# install.sh - Install GPU passthrough configs
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
GPU_BLACKLIST_PATH="/etc/modprobe.d/homelab-gpu-blacklist.conf"
LEGACY_BLACKLIST_PATH="/etc/modprobe.d/blacklist.conf"
VFIO_CONF_PATH="/etc/modprobe.d/vfio.conf"
VFIO_MODULES_PATH="/etc/modules-load.d/vfio.conf"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$BUILD_DIR/cmdline" "$BUILD_DIR/cmdline" || exit 1

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
if [[ -f "$LEGACY_BLACKLIST_PATH" ]] && grep -qE '^blacklist (i915|nvidia|nouveau)' "$LEGACY_BLACKLIST_PATH"; then
    backup_config "$LEGACY_BLACKLIST_PATH"
    sed -i \
        -e 's/^blacklist i915/# &  # Migrated by pve-gpu-passthrough/' \
        -e 's/^blacklist nvidia/# &  # Migrated by pve-gpu-passthrough/' \
        -e 's/^blacklist nouveau/# &  # Migrated by pve-gpu-passthrough/' \
        "$LEGACY_BLACKLIST_PATH"
    initramfs_needs_update=true
fi

if [[ -f "$BUILD_DIR/blacklist.conf" ]]; then
    if [[ ! -f "$GPU_BLACKLIST_PATH" ]] || ! cmp -s "$BUILD_DIR/blacklist.conf" "$GPU_BLACKLIST_PATH"; then
        cp "$BUILD_DIR/blacklist.conf" "$GPU_BLACKLIST_PATH"
        initramfs_needs_update=true
    fi
elif [[ -f "$GPU_BLACKLIST_PATH" ]]; then
    backup_config "$GPU_BLACKLIST_PATH"
    rm -f "$GPU_BLACKLIST_PATH"
    initramfs_needs_update=true
fi

if [[ -f "$BUILD_DIR/vfio.conf" ]]; then
    if [[ ! -f "$VFIO_CONF_PATH" ]] || ! cmp -s "$BUILD_DIR/vfio.conf" "$VFIO_CONF_PATH"; then
        cp "$BUILD_DIR/vfio.conf" "$VFIO_CONF_PATH"
        initramfs_needs_update=true
    fi
elif [[ -f "$VFIO_CONF_PATH" ]]; then
    backup_config "$VFIO_CONF_PATH"
    rm -f "$VFIO_CONF_PATH"
    initramfs_needs_update=true
fi

print_sub "Deploying VFIO modules..."
if [[ -f "$BUILD_DIR/modules" ]]; then
    if [[ ! -f "$VFIO_MODULES_PATH" ]] || ! cmp -s "$BUILD_DIR/modules" "$VFIO_MODULES_PATH"; then
        cp "$BUILD_DIR/modules" "$VFIO_MODULES_PATH"
        initramfs_needs_update=true
    fi
elif [[ -f "$VFIO_MODULES_PATH" ]]; then
    backup_config "$VFIO_MODULES_PATH"
    rm -f "$VFIO_MODULES_PATH"
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
