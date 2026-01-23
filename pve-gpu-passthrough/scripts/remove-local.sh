#!/bin/bash
# remove-local.sh - Remove GPU passthrough configuration (local execution)
#
# Usage (on Proxmox host):
#   ./remove-local.sh [--dry-run]
#   /root/pve-gpu-passthrough-remove.sh [--dry-run]
#
# This script runs ONLY on the local Proxmox host.
# For remote orchestration, use ../remove.sh

set -e

VERSION="2.0.0"
TIMESTAMP=$(date +%Y%m%d%H%M%S)
DRY_RUN=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

show_help() {
    cat << 'EOF'
Usage: remove-local.sh [--dry-run]

Remove GPU passthrough configuration from this Proxmox host.

Options:
  --dry-run, -n   Preview changes without executing
  --help, -h      Show this help
  --version       Show script version
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        --version)
            echo "v$VERSION"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

if [[ ! -d /etc/pve ]]; then
    echo "ERROR: This script must run on a Proxmox VE host"
    echo "       Missing /etc/pve directory"
    exit 1
fi

echo "=== Removing GPU Passthrough Configuration ==="
echo "    Host: $(hostname)"
echo "    Date: $TIMESTAMP"
if [[ "$DRY_RUN" == "true" ]]; then
    echo "    Mode: DRY RUN"
fi
echo ""

echo "==> Updating /etc/kernel/cmdline..."
if [[ ! -f /etc/kernel/cmdline ]]; then
    echo "    Error: /etc/kernel/cmdline not found (systemd-boot required)"
    exit 1
fi

CURRENT_CMDLINE=$(cat /etc/kernel/cmdline)
NEW_CMDLINE=$(echo "$CURRENT_CMDLINE" | sed 's/ video=efifb:off//g' | sed 's/video=efifb:off //g')

if [[ "$CURRENT_CMDLINE" == "$NEW_CMDLINE" ]]; then
    echo "    No changes needed (video=efifb:off not present)"
else
    echo "    Old: $CURRENT_CMDLINE"
    echo "    New: $NEW_CMDLINE"

    if [[ "$DRY_RUN" == "false" ]]; then
        echo "$NEW_CMDLINE" > /etc/kernel/cmdline
        echo "    Updated"
    else
        echo "    [DRY RUN] Would update"
    fi
fi
echo ""

echo "==> Updating /etc/modprobe.d/blacklist.conf..."
if [[ -f /etc/modprobe.d/blacklist.conf ]]; then
    BLACKLIST_CHANGED=false

    if grep -q "^blacklist i915" /etc/modprobe.d/blacklist.conf 2>/dev/null; then
        echo "    Found: blacklist i915"
        BLACKLIST_CHANGED=true
    fi

    if grep -q "^blacklist nvidia" /etc/modprobe.d/blacklist.conf 2>/dev/null; then
        echo "    Found: blacklist nvidia"
        BLACKLIST_CHANGED=true
    fi

    if grep -q "^blacklist nouveau" /etc/modprobe.d/blacklist.conf 2>/dev/null; then
        echo "    Found: blacklist nouveau"
        BLACKLIST_CHANGED=true
    fi

    if [[ "$BLACKLIST_CHANGED" == "true" ]]; then
        if [[ "$DRY_RUN" == "false" ]]; then
            backup_config /etc/modprobe.d/blacklist.conf
            sed -i \
                -e "s/^blacklist i915$/# blacklist i915  # Removed by remove-local.sh on $TIMESTAMP/" \
                -e "s/^blacklist nvidia\*$/# blacklist nvidia*  # Removed by remove-local.sh on $TIMESTAMP/" \
                -e "s/^blacklist nvidia$/# blacklist nvidia  # Removed by remove-local.sh on $TIMESTAMP/" \
                -e "s/^blacklist nouveau$/# blacklist nouveau  # Removed by remove-local.sh on $TIMESTAMP/" \
                /etc/modprobe.d/blacklist.conf
            echo "    Commented out GPU driver blacklists"
        else
            echo "    [DRY RUN] Would comment out blacklists"
        fi
    else
        echo "    No active blacklists found"
    fi
else
    echo "    File not found (skipping)"
fi
echo ""

echo "==> Updating /etc/modprobe.d/vfio.conf..."
if [[ -f /etc/modprobe.d/vfio.conf ]]; then
    if grep -q "^options vfio-pci" /etc/modprobe.d/vfio.conf 2>/dev/null; then
        VFIO_LINE=$(grep "^options vfio-pci" /etc/modprobe.d/vfio.conf)
        echo "    Found: $VFIO_LINE"

        if [[ "$DRY_RUN" == "false" ]]; then
            backup_config /etc/modprobe.d/vfio.conf
            sed -i \
                "s/^options vfio-pci/# options vfio-pci/; s/$/ # Removed by remove-local.sh on $TIMESTAMP/" \
                /etc/modprobe.d/vfio.conf
            echo "    Commented out VFIO device binding"
        else
            echo "    [DRY RUN] Would comment out VFIO binding"
        fi
    else
        echo "    No active VFIO binding found"
    fi
else
    echo "    File not found (skipping)"
fi
echo ""

echo "==> Removing /etc/modules-load.d/vfio.conf..."
if [[ -f /etc/modules-load.d/vfio.conf ]]; then
    if [[ "$DRY_RUN" == "false" ]]; then
        rm -f /etc/modules-load.d/vfio.conf
        echo "    Removed"
    else
        echo "    [DRY RUN] Would remove"
    fi
else
    echo "    File not found (already removed or never deployed)"
fi
echo ""

echo "==> Updating initramfs..."
if [[ "$DRY_RUN" == "false" ]]; then
    update-initramfs -u -k all
    echo "    Complete"
else
    echo "    [DRY RUN] Would run: update-initramfs -u -k all"
fi
echo ""

echo "==> Refreshing systemd-boot..."
if [[ "$DRY_RUN" == "false" ]]; then
    proxmox-boot-tool refresh
    echo "    Complete"
else
    echo "    [DRY RUN] Would run: proxmox-boot-tool refresh"
fi
echo ""

if [[ "$DRY_RUN" == "true" ]]; then
    echo "=== Dry Run Complete - No Changes Made ==="
else
    echo "=== GPU Passthrough Removal Complete ==="
fi
echo ""
echo "IMPORTANT: Reboot required to apply changes:"
echo "  reboot"
