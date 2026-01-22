#!/bin/bash
# remove.sh - Remove GPU passthrough configuration to restore console display
# 
# Usage (Remote from helm):
#   ./remove.sh ace              # Remove from ace
#   ./remove.sh bray             # Remove from bray
#   ./remove.sh clovis           # Remove from clovis
#   ./remove.sh all              # Remove from all hosts
#   ./remove.sh --yes ace        # Skip confirmation
#   ./remove.sh --dry-run ace    # Preview changes
#
# Usage (Local on Proxmox host):
#   /root/pve-gpu-passthrough-remove.sh
#   ~/pve-gpu-passthrough-remove.sh
#
# WARNING: Requires reboot to take effect
# After reboot, GPU will use native driver (i915/nouveau) and display will work

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_LIB="$SCRIPT_DIR/../lib/common.sh"
HOSTS_CONF="$SCRIPT_DIR/hosts.conf"

REMOTE_MODE=false
if [[ -f "$COMMON_LIB" ]]; then
    REMOTE_MODE=true
    source "$COMMON_LIB"

    if [[ -f "$HOSTS_CONF" ]]; then
        if ! load_hosts_file "$HOSTS_CONF"; then
            echo "ERROR: Failed to load hosts registry"
            exit 1
        fi
    else
        echo "ERROR: hosts.conf not found at $HOSTS_CONF"
        exit 1
    fi
fi

# Script metadata
VERSION="1.0.0"
TIMESTAMP=$(date +%Y-%m-%d)

# Detect execution mode
function is_proxmox_host() {
    [[ -d /etc/pve ]]
}

function detect_mode() {
    if is_proxmox_host; then
        echo "local"
    else
        echo "remote"
    fi
}

# Detect if running as deployed script on host
function is_deployed_script() {
    local script_path="$(readlink -f "$0")"
    if [[ "$script_path" == "/root/pve-gpu-passthrough-remove.sh" ]]; then
        return 0  # True - deployed script
    else
        return 1  # False - repo script
    fi
}

# Show help
function show_help() {
    cat << 'EOF'
GPU Passthrough Removal Script

Usage (Remote from helm):
  ./remove.sh <hostname|all>       Remove GPU passthrough from host(s)
  ./remove.sh --yes ace            Skip confirmation prompt
  ./remove.sh --dry-run ace        Preview changes without executing

Usage (Local on Proxmox host):
  /root/pve-gpu-passthrough-remove.sh
  ~/pve-gpu-passthrough-remove.sh

Supported hosts: Any Proxmox VE node in the homelab registry (type: pve)

What it does:
  - Removes video=efifb:off from kernel cmdline (restores framebuffer)
  - Comments out GPU driver blacklists (i915, nvidia, nouveau)
  - Comments out VFIO device binding
  - Removes VFIO module config
  - Updates initramfs and bootloader
  - Preserves IOMMU/ACS kernel parameters

After running: Reboot required to restore display output
EOF
}

# Main removal function (works on local system)
function remove_gpu_passthrough_local() {
    local current_host=$(hostname)
    
    # Check for dry-run flag file (used when called remotely)
    if [[ -f /tmp/pve-gpu-passthrough-dryrun.flag ]]; then
        DRY_RUN="true"
    fi
    
    # Validate this is a Proxmox host
    if ! is_proxmox_host; then
        echo "ERROR: This script must run on a Proxmox VE host"
        echo "       Missing /etc/pve directory"
        exit 1
    fi
    
    echo "=== Removing GPU Passthrough Configuration ==="
    echo "    Host: $current_host"
    echo "    Mode: Local execution (standalone)"
    echo "    Date: $TIMESTAMP"
    echo ""
    
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        echo "    DRY RUN MODE - No changes will be made"
        echo ""
    fi
    
    # 1. Update kernel cmdline (remove video=efifb:off)
    echo "==> Updating /etc/kernel/cmdline..."
    if [[ ! -f /etc/kernel/cmdline ]]; then
        echo "    ✗ Error: /etc/kernel/cmdline not found (systemd-boot required)"
        exit 1
    fi
    
    CURRENT_CMDLINE=$(cat /etc/kernel/cmdline)
    NEW_CMDLINE=$(echo "$CURRENT_CMDLINE" | sed 's/ video=efifb:off//g' | sed 's/video=efifb:off //g')
    
    if [[ "$CURRENT_CMDLINE" == "$NEW_CMDLINE" ]]; then
        echo "    ℹ No changes needed (video=efifb:off not present)"
    else
        echo "    Old: $CURRENT_CMDLINE"
        echo "    New: $NEW_CMDLINE"
        
        if [[ "${DRY_RUN:-false}" == "false" ]]; then
            echo "$NEW_CMDLINE" > /etc/kernel/cmdline
            echo "    ✓ Updated"
        else
            echo "    [DRY RUN] Would update"
        fi
    fi
    echo ""
    
    # 2. Comment out driver blacklists
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
            if [[ "${DRY_RUN:-false}" == "false" ]]; then
                sed -i.bak."$TIMESTAMP" \
                    -e "s/^blacklist i915$/# blacklist i915  # Removed by remove.sh on $TIMESTAMP/" \
                    -e "s/^blacklist nvidia\*$/# blacklist nvidia*  # Removed by remove.sh on $TIMESTAMP/" \
                    -e "s/^blacklist nvidia$/# blacklist nvidia  # Removed by remove.sh on $TIMESTAMP/" \
                    -e "s/^blacklist nouveau$/# blacklist nouveau  # Removed by remove.sh on $TIMESTAMP/" \
                    /etc/modprobe.d/blacklist.conf
                echo "    ✓ Commented out GPU driver blacklists"
            else
                echo "    [DRY RUN] Would comment out blacklists"
            fi
        else
            echo "    ℹ No active blacklists found"
        fi
    else
        echo "    ℹ File not found (skipping)"
    fi
    echo ""
    
    # 3. Comment out VFIO device binding
    echo "==> Updating /etc/modprobe.d/vfio.conf..."
    if [[ -f /etc/modprobe.d/vfio.conf ]]; then
        if grep -q "^options vfio-pci" /etc/modprobe.d/vfio.conf 2>/dev/null; then
            VFIO_LINE=$(grep "^options vfio-pci" /etc/modprobe.d/vfio.conf)
            echo "    Found: $VFIO_LINE"
            
            if [[ "${DRY_RUN:-false}" == "false" ]]; then
                sed -i.bak."$TIMESTAMP" \
                    "s/^options vfio-pci/# options vfio-pci/; s/$/ # Removed by remove.sh on $TIMESTAMP/" \
                    /etc/modprobe.d/vfio.conf
                echo "    ✓ Commented out VFIO device binding"
            else
                echo "    [DRY RUN] Would comment out VFIO binding"
            fi
        else
            echo "    ℹ No active VFIO binding found"
        fi
    else
        echo "    ℹ File not found (skipping)"
    fi
    echo ""
    
    # 4. Remove VFIO modules config
    echo "==> Removing /etc/modules-load.d/vfio.conf..."
    if [[ -f /etc/modules-load.d/vfio.conf ]]; then
        if [[ "${DRY_RUN:-false}" == "false" ]]; then
            rm -f /etc/modules-load.d/vfio.conf
            echo "    ✓ Removed"
        else
            echo "    [DRY RUN] Would remove"
        fi
    else
        echo "    ℹ File not found (already removed or never deployed)"
    fi
    echo ""
    
    # 5. Update initramfs
    echo "==> Updating initramfs..."
    if [[ "${DRY_RUN:-false}" == "false" ]]; then
        update-initramfs -u -k all
        echo "    ✓ Complete"
    else
        echo "    [DRY RUN] Would run: update-initramfs -u -k all"
    fi
    echo ""
    
    # 6. Refresh bootloader
    echo "==> Refreshing systemd-boot..."
    if [[ "${DRY_RUN:-false}" == "false" ]]; then
        proxmox-boot-tool refresh
        echo "    ✓ Complete"
    else
        echo "    [DRY RUN] Would run: proxmox-boot-tool refresh"
    fi
    echo ""
    
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        echo "=== Dry Run Complete - No Changes Made ==="
    else
        echo "=== GPU Passthrough Removal Complete ==="
    fi
    echo ""
    echo "IMPORTANT: Reboot required to apply changes:"
    echo "  reboot"
    echo ""
    echo "After reboot:"
    echo "  - Display output will be restored"
    echo "  - GPU will use native driver (i915/nouveau)"
    echo "  - GPU passthrough will be disabled"
    echo ""
    echo "To re-enable GPU passthrough:"
    echo "  cd ~/homelab/pve-gpu-passthrough"
    echo "  ./deploy.sh $current_host"
    echo "  reboot"
}

# Remote execution wrapper (SSH to target)
function remove_gpu_passthrough_remote() {
    local host=$1
    
    echo "=== Removing GPU Passthrough from $host ==="
    echo ""
    
    # Check if host is reachable
    if ! ssh -o ConnectTimeout=5 "$host" "echo 'Connected' > /dev/null" 2>/dev/null; then
        echo "    ✗ Error: Cannot connect to $host via SSH"
        return 1
    fi
    
    # Copy script to target
    echo "    Copying removal script to $host:/tmp/..."
    SCRIPT_PATH="$(readlink -f "$0")"
    scp -q "$SCRIPT_PATH" "$host:/tmp/pve-gpu-passthrough-remove-temp.sh"
    
    # Execute in local mode on target
    echo "    Executing removal on $host..."
    echo ""
    
    # Pass DRY_RUN flag via temporary flag file
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        ssh "$host" 'echo "true" > /tmp/pve-gpu-passthrough-dryrun.flag && bash /tmp/pve-gpu-passthrough-remove-temp.sh'
    else
        ssh "$host" 'rm -f /tmp/pve-gpu-passthrough-dryrun.flag && bash /tmp/pve-gpu-passthrough-remove-temp.sh'
    fi
    
    # Cleanup
    ssh "$host" "rm -f /tmp/pve-gpu-passthrough-remove-temp.sh /tmp/pve-gpu-passthrough-dryrun.flag"
    
    echo ""
    return 0
}

# Parse command-line arguments
DRY_RUN=false
SKIP_CONFIRM=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --yes|-y)
            SKIP_CONFIRM=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        --version)
            echo "GPU Passthrough Removal Script v$VERSION"
            exit 0
            ;;
        *)
            # Remaining args are hostnames
            break
            ;;
    esac
done

# Main execution logic
MODE=$(detect_mode)

if [[ "$MODE" == "local" ]]; then
    # Running on Proxmox host directly
    if [[ $# -gt 0 ]]; then
        echo "INFO: Running in local mode - arguments ignored"
        echo "      Will only modify current host: $(hostname)"
        echo ""
    fi
    remove_gpu_passthrough_local
    
elif is_deployed_script; then
    # Deployed script called from wrong context
    echo "ERROR: This script is deployed on a Proxmox host for local emergency use only"
    echo ""
    echo "For remote deployment, use the script from the homelab repo:"
    echo "  cd ~/homelab/pve-gpu-passthrough"
    echo "  ./remove.sh <hostname|all>"
    exit 1
    
else
    # Running from repo - remote mode
    if [[ $# -eq 0 ]]; then
        echo "ERROR: No hostname specified"
        echo ""
        show_help
        exit 1
    fi
    
    # Parse hosts
    if [[ "$REMOTE_MODE" == "false" ]]; then
        if [[ "$1" == "all" ]]; then
            echo "ERROR: 'all' not supported in standalone mode"
            echo "       Run from homelab repo or specify explicit hostname"
            exit 1
        fi
        HOSTS=("$@")
    else
        if [[ "$1" == "all" ]]; then
            HOSTS=($(get_hosts_by_type pve))
            if [[ ${#HOSTS[@]} -eq 0 ]]; then
                echo "ERROR: No Proxmox hosts found in registry"
                exit 1
            fi
        else
            HOSTS=("$@")
        fi

        # Validate each host is type pve
        for host in "${HOSTS[@]}"; do
            host_type=$(get_host_type "$host" 2>/dev/null || echo "")
            if [[ "$host_type" != "pve" ]]; then
                echo "ERROR: Host '$host' is not a Proxmox node (type=${host_type:-unknown})"
                echo "       Only Proxmox VE hosts are supported"
                exit 1
            fi
        done
    fi
    
    # Show plan and confirm (unless --yes or --dry-run)
    if [[ "$SKIP_CONFIRM" == "false" && "$DRY_RUN" == "false" ]]; then
        echo "=== GPU Passthrough Removal Plan ==="
        if [[ "$REMOTE_MODE" == "true" ]]; then
            echo "    Hosts: ${HOSTS[*]}"
            echo "    (All hosts validated as type=pve)"
        else
            echo "    Hosts: ${HOSTS[*]} (not validated - standalone mode)"
        fi
        echo ""
        echo "    Changes per host:"
        echo "    - Remove video=efifb:off from kernel cmdline"
        echo "    - Comment out GPU driver blacklists"
        echo "    - Comment out VFIO device bindings"
        echo "    - Remove VFIO module config"
        echo "    - Update initramfs and bootloader"
        echo ""
        echo "    IOMMU/ACS settings will be PRESERVED"
        echo ""
        read -p "Proceed with removal? [y/N]: " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Cancelled."
            exit 0
        fi
        echo ""
    fi
    
    # Execute removal on each host
    FAILED_HOSTS=()
    for host in "${HOSTS[@]}"; do
        if ! remove_gpu_passthrough_remote "$host"; then
            FAILED_HOSTS+=("$host")
        fi
    done
    
    # Summary
    echo "=== Removal Complete ==="
    echo ""
    
    if [[ ${#FAILED_HOSTS[@]} -gt 0 ]]; then
        echo "✗ Failed hosts: ${FAILED_HOSTS[*]}"
        echo ""
    fi
    
    # Show reboot instructions
    SUCCESS_HOSTS=()
    for host in "${HOSTS[@]}"; do
        if [[ ! " ${FAILED_HOSTS[*]} " =~ " $host " ]]; then
            SUCCESS_HOSTS+=("$host")
        fi
    done
    
    if [[ ${#SUCCESS_HOSTS[@]} -gt 0 ]]; then
        if [[ "$DRY_RUN" == "false" ]]; then
            echo "IMPORTANT: Reboot required to apply changes:"
            for host in "${SUCCESS_HOSTS[@]}"; do
                echo "  ssh $host reboot"
            done
            echo ""
            echo "After reboot:"
            echo "  - Display output will be restored on all hosts"
            echo "  - GPUs will use native drivers (i915/nouveau)"
            echo "  - GPU passthrough will be disabled"
        fi
    fi
    
    if [[ ${#FAILED_HOSTS[@]} -gt 0 ]]; then
        exit 1
    fi
fi
