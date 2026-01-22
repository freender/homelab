#!/bin/bash
# install.sh - Install GPU passthrough configs
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ ! -f "$BUILD_DIR/blacklist.conf" || ! -f "$BUILD_DIR/cmdline" || ! -f "$BUILD_DIR/vfio.conf" || ! -f "$BUILD_DIR/modules" ]]; then
    echo "Error: Missing build artifacts in $BUILD_DIR"
    exit 1
fi

if [[ ! -f /etc/kernel/cmdline ]]; then
    echo "Error: /etc/kernel/cmdline not found (systemd-boot required)"
    exit 1
fi

print_sub() { echo "    $*"; }

print_sub "Backing up configs..."
cp /etc/kernel/cmdline /etc/kernel/cmdline.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true
cp /etc/modules /etc/modules.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true

print_sub "Updating systemd-boot cmdline..."
cmdline=$(cat "$BUILD_DIR/cmdline")
current_cmdline=$(cat /etc/kernel/cmdline)
[[ "$current_cmdline" != *"root="* ]] && current_cmdline=$(cat /proc/cmdline)

filtered=""
seen=""
for arg in $current_cmdline; do
    case "$arg" in
        BOOT_IMAGE=*|initrd=*|intel_iommu=*|amd_iommu=*|iommu=*|pcie_acs_override=*|video=*)
            continue ;;
        *)
            [[ ! " $seen " =~ " $arg " ]] && { filtered="$filtered $arg"; seen="$seen $arg"; }
            ;;
    esac
done

new_cmdline="${filtered# } $cmdline"
final=""
final_seen=""
for arg in $new_cmdline; do
    [[ ! " $final_seen " =~ " $arg " ]] && { final="$final $arg"; final_seen="$final_seen $arg"; }
done
final="${final# }"

printf '%s\n' "$final" > /etc/kernel/cmdline
proxmox-boot-tool refresh

print_sub "Deploying modprobe configs..."
cp "$BUILD_DIR/blacklist.conf" /etc/modprobe.d/blacklist.conf
cp "$BUILD_DIR/vfio.conf" /etc/modprobe.d/vfio.conf

print_sub "Deploying VFIO modules..."
cp "$BUILD_DIR/modules" /etc/modules-load.d/vfio.conf

print_sub "Cleaning legacy /etc/modules..."
if [[ -f /etc/modules ]]; then
    grep -v '^vfio' /etc/modules > /tmp/modules.clean && mv /tmp/modules.clean /etc/modules
fi

print_sub "Deploying emergency removal script..."
if [[ -f "$SCRIPT_DIR/remove.sh" ]]; then
    cp "$SCRIPT_DIR/remove.sh" /root/pve-gpu-passthrough-remove.sh
    chmod +x /root/pve-gpu-passthrough-remove.sh
fi

print_sub "Updating initramfs..."
update-initramfs -u -k all
