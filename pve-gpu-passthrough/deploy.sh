#!/bin/bash
# Deploy GPU passthrough configs to PVE nodes
# Run from helm: ./deploy.sh [ace|clovis|all]

set -e

# Supported hosts for this module
SUPPORTED_HOSTS=("ace" "bray" "clovis")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping pve-gpu-passthrough (not applicable to $1)"
        exit 0
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS="${@:-ace bray clovis}"
HOSTS="${@:-ace bray clovis}"
if [[ "$1" == "all" ]]; then
    HOSTS="ace bray clovis"
fi

echo "==> Deploying GPU Passthrough Configs"
echo "    Hosts: $HOSTS"
echo "    WARNING: This will modify systemd-boot cmdline, modules, and initramfs"
echo ""

for host in $HOSTS; do
    echo "==> Deploying to $host..."

    if [[ ! -d "$SCRIPT_DIR/$host" ]]; then
        echo "    ✗ Error: No config found for node: $host"
        echo "    Available: $(ls -d "$SCRIPT_DIR"/*/ 2>/dev/null | xargs -n1 basename | tr '\n' ' ')"
        continue
    fi

    if [[ ! -f "$SCRIPT_DIR/$host/cmdline" ]]; then
        echo "    ✗ Error: Missing cmdline config for $host"
        continue
    fi

    # Backup existing configs
    echo "    Backing up existing configs..."
    ssh "$host" "cp /etc/kernel/cmdline /etc/kernel/cmdline.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modules /etc/modules.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modprobe.d/blacklist.conf /etc/modprobe.d/blacklist.conf.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modprobe.d/vfio.conf /etc/modprobe.d/vfio.conf.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true"

    # Update boot cmdline (systemd-boot)
    echo "    Updating systemd-boot (/etc/kernel/cmdline)"
    CMDLINE=$(cat "$SCRIPT_DIR/$host/cmdline")

    if ! ssh "$host" "test -f /etc/kernel/cmdline"; then
        echo "    ✗ Error: /etc/kernel/cmdline not found (systemd-boot required)"
        continue
    fi

    CURRENT_CMDLINE=$(ssh "$host" "cat /etc/kernel/cmdline")
    if [[ "$CURRENT_CMDLINE" != *"root="* ]]; then
        CURRENT_CMDLINE=$(ssh "$host" "cat /proc/cmdline")
    fi

    FILTERED_CMDLINE=""
    SEEN_ARGS=""
    for arg in $CURRENT_CMDLINE; do
        case "$arg" in
            BOOT_IMAGE=*|initrd=*|intel_iommu=*|amd_iommu=*|iommu=*|pcie_acs_override=*|video=*)
                continue
                ;;
            *)
                # Deduplicate parameters (especially "quiet")
                if [[ ! " $SEEN_ARGS " =~ " $arg " ]]; then
                    FILTERED_CMDLINE="$FILTERED_CMDLINE $arg"
                    SEEN_ARGS="$SEEN_ARGS $arg"
                fi
                ;;
        esac
    done

    FILTERED_CMDLINE="${FILTERED_CMDLINE# }"
    NEW_CMDLINE="$FILTERED_CMDLINE $CMDLINE"
    NEW_CMDLINE="${NEW_CMDLINE# }"
    
    # Final deduplication after combining (handles duplicates between old and new)
    FINAL_CMDLINE=""
    FINAL_SEEN=""
    for arg in $NEW_CMDLINE; do
        if [[ ! " $FINAL_SEEN " =~ " $arg " ]]; then
            FINAL_CMDLINE="$FINAL_CMDLINE $arg"
            FINAL_SEEN="$FINAL_SEEN $arg"
        fi
    done
    FINAL_CMDLINE="${FINAL_CMDLINE# }"

    ssh "$host" "printf '%s\n' \"$FINAL_CMDLINE\" > /etc/kernel/cmdline"
    ssh "$host" "proxmox-boot-tool refresh"

    # Deploy modprobe configs
    echo "    Deploying modprobe configs..."
    scp "$SCRIPT_DIR/$host/blacklist.conf" "$host":/etc/modprobe.d/blacklist.conf
    scp "$SCRIPT_DIR/$host/vfio.conf" "$host":/etc/modprobe.d/vfio.conf

    # Deploy VFIO modules
    echo "    Deploying VFIO modules (modules-load.d)..."
    scp "$SCRIPT_DIR/$host/modules" "$host":/tmp/vfio-modules
    ssh "$host" "install -D -m 0644 /tmp/vfio-modules /etc/modules-load.d/vfio.conf && rm /tmp/vfio-modules"

    # Clean legacy VFIO module entries
    echo "    Cleaning legacy /etc/modules VFIO entries..."
    ssh "$host" "if [ -f /etc/modules ]; then grep -v '^vfio' /etc/modules > /tmp/modules.clean && mv /tmp/modules.clean /etc/modules; fi"
    ssh "$host" "if [ -f /etc/modules-load.d/modules.conf ]; then grep -v '^vfio' /etc/modules-load.d/modules.conf > /tmp/modules.conf.clean && mv /tmp/modules.conf.clean /etc/modules-load.d/modules.conf; fi"

    # Deploy emergency removal script
    echo "    Deploying emergency removal script..."
    scp "$SCRIPT_DIR/remove.sh" "$host:/root/pve-gpu-passthrough-remove.sh"
    ssh "$host" "chmod 755 /root/pve-gpu-passthrough-remove.sh"
    ssh "$host" "chown root:root /root/pve-gpu-passthrough-remove.sh"

    # Update initramfs
    echo "    Updating initramfs..."
    ssh "$host" "update-initramfs -u -k all"

    echo "    ✓ Deployed to $host (reboot required)"
    echo "    ✓ Emergency removal script: /root/pve-gpu-passthrough-remove.sh"
    echo ""
done

echo "==> Deployment complete!"
echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
