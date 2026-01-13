#!/bin/bash
# Deploy GPU passthrough configs to PVE nodes
# Usage: ./deploy.sh [ace|clovis|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS="${@:-ace clovis}"
if [[ "$1" == "all" ]]; then
    HOSTS="ace clovis"
fi

echo "==> Deploying GPU Passthrough Configs"
echo "    Hosts: $HOSTS"
echo "    WARNING: This will modify GRUB, modules, and initramfs"
echo ""

for host in $HOSTS; do
    echo "==> Deploying to $host..."

    if [[ ! -d "$SCRIPT_DIR/$host" ]]; then
        echo "    ✗ Error: No config found for node: $host"
        echo "    Available: $(ls -d "$SCRIPT_DIR"/*/ 2>/dev/null | xargs -n1 basename | tr '\n' ' ')"
        continue
    fi

    # Backup existing configs
    echo "    Backing up existing configs..."
    ssh "$host" "cp /etc/default/grub /etc/default/grub.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modules /etc/modules.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modprobe.d/blacklist.conf /etc/modprobe.d/blacklist.conf.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modprobe.d/vfio.conf /etc/modprobe.d/vfio.conf.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"

    # Update GRUB cmdline
    echo "    Updating GRUB configuration..."
    GRUB_LINE=$(cat "$SCRIPT_DIR/$host/grub")
    ssh "$host" "sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|$GRUB_LINE|' /etc/default/grub"

    # Deploy modprobe configs
    echo "    Deploying modprobe configs..."
    scp "$SCRIPT_DIR/$host/blacklist.conf" "$host":/etc/modprobe.d/blacklist.conf
    scp "$SCRIPT_DIR/$host/vfio.conf" "$host":/etc/modprobe.d/vfio.conf

    # Check if VFIO modules already in /etc/modules
    echo "    Checking /etc/modules for VFIO entries..."
    if ! ssh "$host" "grep -q '^vfio_pci' /etc/modules"; then
        echo "    Adding VFIO modules to /etc/modules..."
        scp "$SCRIPT_DIR/$host/modules" "$host":/tmp/vfio-modules
        ssh "$host" "cat /tmp/vfio-modules >> /etc/modules && rm /tmp/vfio-modules"
    else
        echo "    VFIO modules already present in /etc/modules"
    fi

    # Update grub and initramfs
    echo "    Updating GRUB and initramfs..."
    ssh "$host" "update-grub && update-initramfs -u -k all"

    echo "    ✓ Deployed to $host (reboot required)"
    echo ""
done

echo "==> Deployment complete!"
echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
