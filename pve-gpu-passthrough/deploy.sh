#!/bin/bash
# Deploy GPU passthrough configs to PVE nodes
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
PROFILES_DIR="$SCRIPT_DIR/profiles"
MODULES_FILE="$SCRIPT_DIR/modules"

# --- Host Selection ---
SUPPORTED_HOSTS=($(hosts list --feature gpu))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-gpu-passthrough (not applicable to $1)"
    exit 0
fi

# --- Validation ---
[[ ! -f "$MODULES_FILE" ]] && { echo "Error: modules file not found"; exit 1; }

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local profile pci_ids
    
    profile=$(hosts get "$host" "gpu.profile") || { print_warn "gpu.profile missing"; return 1; }
    pci_ids=$(hosts get "$host" "gpu.pci_ids") || { print_warn "gpu.pci_ids missing"; return 1; }
    
    [[ ! -d "$PROFILES_DIR/$profile" ]] && { print_warn "Unknown profile: $profile"; return 1; }
    
    local blacklist_conf="$PROFILES_DIR/$profile/blacklist.conf"
    local cmdline_conf="$PROFILES_DIR/$profile/cmdline"
    local vfio_template="$PROFILES_DIR/$profile/vfio.conf.tpl"
    
    if [[ ! -f "$blacklist_conf" || ! -f "$cmdline_conf" || ! -f "$vfio_template" ]]; then
        print_warn "Missing profile config for $profile"
        return 1
    fi
    
    # Render vfio.conf
    local vfio_tmp vfio_content
    vfio_tmp=$(mktemp)
    vfio_content=$(cat "$vfio_template")
    printf '%s\n' "${vfio_content//\$\{PCI_IDS\}/$pci_ids}" > "$vfio_tmp"
    
    print_sub "Backing up configs..."
    ssh "$host" "cp /etc/kernel/cmdline /etc/kernel/cmdline.bak.\$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modules /etc/modules.bak.\$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    
    print_sub "Updating systemd-boot cmdline..."
    local cmdline current_cmdline
    cmdline=$(cat "$cmdline_conf")
    
    if ! ssh "$host" "test -f /etc/kernel/cmdline"; then
        print_warn "/etc/kernel/cmdline not found (systemd-boot required)"
        rm -f "$vfio_tmp"
        return 1
    fi
    
    current_cmdline=$(ssh "$host" "cat /etc/kernel/cmdline")
    [[ "$current_cmdline" != *"root="* ]] && current_cmdline=$(ssh "$host" "cat /proc/cmdline")
    
    # Filter out IOMMU/GPU args
    local filtered="" seen=""
    for arg in $current_cmdline; do
        case "$arg" in
            BOOT_IMAGE=*|initrd=*|intel_iommu=*|amd_iommu=*|iommu=*|pcie_acs_override=*|video=*)
                continue ;;
            *)
                [[ ! " $seen " =~ " $arg " ]] && { filtered="$filtered $arg"; seen="$seen $arg"; }
                ;;
        esac
    done
    
    local new_cmdline="${filtered# } $cmdline"
    local final="" final_seen=""
    for arg in $new_cmdline; do
        [[ ! " $final_seen " =~ " $arg " ]] && { final="$final $arg"; final_seen="$final_seen $arg"; }
    done
    final="${final# }"
    
    ssh "$host" "printf '%s\n' \"$final\" > /etc/kernel/cmdline"
    ssh "$host" "proxmox-boot-tool refresh"
    
    print_sub "Deploying modprobe configs..."
    deploy_file "$blacklist_conf" "$host" "/etc/modprobe.d/blacklist.conf"
    deploy_file "$vfio_tmp" "$host" "/etc/modprobe.d/vfio.conf"
    
    print_sub "Deploying VFIO modules..."
    deploy_file "$MODULES_FILE" "$host" "/etc/modules-load.d/vfio.conf"
    
    print_sub "Cleaning legacy /etc/modules..."
    ssh "$host" "if [ -f /etc/modules ]; then grep -v '^vfio' /etc/modules > /tmp/modules.clean && mv /tmp/modules.clean /etc/modules; fi"
    
    print_sub "Deploying emergency removal script..."
    deploy_script "$SCRIPT_DIR/remove.sh" "$host" "/root/pve-gpu-passthrough-remove.sh"
    
    print_sub "Updating initramfs..."
    ssh "$host" "update-initramfs -u -k all"
    
    rm -f "$vfio_tmp"
    
    print_sub "Reboot required"
    print_ok "Emergency removal: /root/pve-gpu-passthrough-remove.sh"
}

# --- Main ---
print_sub "WARNING: This will modify systemd-boot cmdline, modules, and initramfs"
deploy_init "GPU Passthrough Configs"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
