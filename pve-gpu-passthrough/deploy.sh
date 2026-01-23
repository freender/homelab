#!/bin/bash
# Deploy GPU passthrough configs to PVE nodes
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
MODULES_FILE="$SCRIPT_DIR/configs/modules"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

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
    local build_dir="$BUILD_ROOT/$host"

    profile=$(hosts get "$host" "gpu.profile") || { print_warn "gpu.profile missing"; return 1; }
    pci_ids=$(hosts get "$host" "gpu.pci_ids") || { print_warn "gpu.pci_ids missing"; return 1; }

    [[ ! -d "$TEMPLATES_DIR/$profile" ]] && { print_warn "Unknown profile: $profile"; return 1; }

    local blacklist_conf="$TEMPLATES_DIR/$profile/blacklist.conf"
    local cmdline_conf="$TEMPLATES_DIR/$profile/cmdline"
    local vfio_template="$TEMPLATES_DIR/$profile/vfio.conf.tpl"

    if [[ ! -f "$blacklist_conf" || ! -f "$cmdline_conf" || ! -f "$vfio_template" ]]; then
        print_warn "Missing profile config for $profile"
        return 1
    fi

    prepare_build_dir "$build_dir"

    cp "$blacklist_conf" "$build_dir/blacklist.conf"
    cp "$cmdline_conf" "$build_dir/cmdline"
    cp "$MODULES_FILE" "$build_dir/modules"

    render_template "$vfio_template" "$build_dir/vfio.conf" PCI_IDS="$pci_ids"

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-gpu-passthrough/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-gpu-passthrough && mkdir -p /tmp/homelab-pve-gpu-passthrough/build /tmp/homelab-pve-gpu-passthrough/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-gpu-passthrough/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$SCRIPT_DIR/remove.sh" "$host:/tmp/homelab-pve-gpu-passthrough/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-gpu-passthrough/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-gpu-passthrough && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
print_sub "WARNING: This will modify systemd-boot cmdline, modules, and initramfs"
deploy_init "GPU Passthrough Configs"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
