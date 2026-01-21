#!/bin/bash
# Deploy GPU passthrough configs to PVE nodes
# Usage: ./deploy.sh [ace|bray|clovis|all]

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILES_DIR="$SCRIPT_DIR/profiles"
MODULES_FILE="$SCRIPT_DIR/modules"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

get_gpu_hosts() {
    get_hosts_with_feature gpu
}

load_host_config() {
    local host="$1"
    PROFILE=$(get_host_kv "$host" "gpu.profile")
    PCI_IDS=$(get_host_kv "$host" "gpu.pci_ids")

    if [[ -z "$PROFILE" || -z "$PCI_IDS" ]]; then
        print_warn "Missing gpu.profile or gpu.pci_ids for $host in pve-gpu-passthrough/hosts.conf"
        return 1
    fi
}

SUPPORTED_HOSTS=($(get_gpu_hosts))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-gpu-passthrough (not applicable to $1)"
    exit 0
fi

if [[ ! -f "$MODULES_FILE" ]]; then
    echo "Error: modules file not found at $MODULES_FILE"
    exit 1
fi

print_action "Deploying GPU Passthrough Configs"
print_sub "Hosts: $HOSTS"
print_sub "WARNING: This will modify systemd-boot cmdline, modules, and initramfs"
echo ""

for host in $HOSTS; do
    print_action "Deploying to $host..."

    if ! load_host_config "$host"; then
        continue
    fi

    if [[ ! -d "$PROFILES_DIR/$PROFILE" ]]; then
        print_warn "Unknown profile for $host: $PROFILE"
        continue
    fi

    blacklist_conf="$PROFILES_DIR/$PROFILE/blacklist.conf"
    cmdline_conf="$PROFILES_DIR/$PROFILE/cmdline"
    vfio_template="$PROFILES_DIR/$PROFILE/vfio.conf.tpl"

    if [[ ! -f "$blacklist_conf" || ! -f "$cmdline_conf" || ! -f "$vfio_template" ]]; then
        print_warn "Missing profile config for $host ($PROFILE)"
        continue
    fi

    # Render vfio.conf from template
    vfio_tmp=$(mktemp)
    vfio_content=$(cat "$vfio_template")
    printf '%s\n' "${vfio_content//\$\{PCI_IDS\}/$PCI_IDS}" > "$vfio_tmp"

    # Backup existing configs
    print_sub "Backing up existing configs..."
    ssh "$host" "cp /etc/kernel/cmdline /etc/kernel/cmdline.bak.\$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modules /etc/modules.bak.\$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modprobe.d/blacklist.conf /etc/modprobe.d/blacklist.conf.bak.\$(date +%Y%m%d%H%M%S) 2>/dev/null || true"
    ssh "$host" "cp /etc/modprobe.d/vfio.conf /etc/modprobe.d/vfio.conf.bak.\$(date +%Y%m%d%H%M%S) 2>/dev/null || true"

    # Update boot cmdline (systemd-boot)
    print_sub "Updating systemd-boot (/etc/kernel/cmdline)"
    CMDLINE=$(cat "$cmdline_conf")

    if ! ssh "$host" "test -f /etc/kernel/cmdline"; then
        print_warn "/etc/kernel/cmdline not found (systemd-boot required)"
        rm -f "$vfio_tmp"
        continue
    fi

    CURRENT_CMDLINE=$(ssh "$host" "cat /etc/kernel/cmdline")
    if [[ "$CURRENT_CMDLINE" != *"root="* ]]; then
        CURRENT_CMDLINE=$(ssh "$host" "cat /proc/cmdline")
    fi

    # Filter out IOMMU/GPU-related args from current cmdline
    FILTERED_CMDLINE=""
    SEEN_ARGS=""
    for arg in $CURRENT_CMDLINE; do
        case "$arg" in
            BOOT_IMAGE=*|initrd=*|intel_iommu=*|amd_iommu=*|iommu=*|pcie_acs_override=*|video=*)
                continue
                ;;
            *)
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

    # Final deduplication
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
    print_sub "Deploying modprobe configs..."
    deploy_file "$blacklist_conf" "$host" "/etc/modprobe.d/blacklist.conf"
    deploy_file "$vfio_tmp" "$host" "/etc/modprobe.d/vfio.conf"

    # Deploy VFIO modules
    print_sub "Deploying VFIO modules (modules-load.d)..."
    deploy_file "$MODULES_FILE" "$host" "/etc/modules-load.d/vfio.conf"

    # Clean legacy VFIO module entries
    print_sub "Cleaning legacy /etc/modules VFIO entries..."
    ssh "$host" "if [ -f /etc/modules ]; then grep -v '^vfio' /etc/modules > /tmp/modules.clean && mv /tmp/modules.clean /etc/modules; fi"
    ssh "$host" "if [ -f /etc/modules-load.d/modules.conf ]; then grep -v '^vfio' /etc/modules-load.d/modules.conf > /tmp/modules.conf.clean && mv /tmp/modules.conf.clean /etc/modules-load.d/modules.conf; fi"

    # Deploy emergency removal script
    print_sub "Deploying emergency removal script..."
    deploy_script "$SCRIPT_DIR/remove.sh" "$host" "/root/pve-gpu-passthrough-remove.sh"

    # Update initramfs
    print_sub "Updating initramfs..."
    ssh "$host" "update-initramfs -u -k all"

    rm -f "$vfio_tmp"

    print_ok "Deployed to $host (reboot required)"
    print_ok "Emergency removal script: /root/pve-gpu-passthrough-remove.sh"
    echo ""
done

print_action "Deployment complete!"
echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
