#!/bin/bash
# Deploy GPU passthrough configs to PVE nodes
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULES_FILE="$SCRIPT_DIR/configs/modules"
BLACKLIST_FILE="$SCRIPT_DIR/configs/blacklist.conf"
CMDLINE_FILE="$SCRIPT_DIR/configs/cmdline"
VFIO_TEMPLATE="$SCRIPT_DIR/configs/vfio.conf.tpl"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-gpu-passthrough)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-gpu-passthrough (not applicable to $1)"
    exit 0
fi

# --- Validation ---
[[ ! -f "$MODULES_FILE" ]] && { echo "Error: modules file not found"; exit 1; }
[[ ! -f "$BLACKLIST_FILE" ]] && { echo "Error: blacklist file not found"; exit 1; }
[[ ! -f "$CMDLINE_FILE" ]] && { echo "Error: cmdline file not found"; exit 1; }
[[ ! -f "$VFIO_TEMPLATE" ]] && { echo "Error: vfio template not found"; exit 1; }

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local pci_ids
    local build_dir="$BUILD_ROOT/$host"

    pci_ids=$(hosts get "$host" "pve-gpu-passthrough.pci_ids") || { print_warn "pve-gpu-passthrough.pci_ids missing"; return 1; }

    if [[ ! -f "$BLACKLIST_FILE" || ! -f "$CMDLINE_FILE" || ! -f "$VFIO_TEMPLATE" ]]; then
        print_warn "Missing static config inputs in $SCRIPT_DIR/configs"
        return 1
    fi

    prepare_build_dir "$build_dir"

    cp "$BLACKLIST_FILE" "$build_dir/blacklist.conf"
    cp "$CMDLINE_FILE" "$build_dir/cmdline"
    cp "$MODULES_FILE" "$build_dir/modules"

    render_template "$VFIO_TEMPLATE" "$build_dir/vfio.conf" PCI_IDS="$pci_ids"

    print_sub "Comparing with remote configs..."
    diff_remote_config "$host" "$build_dir/blacklist.conf" "/etc/modprobe.d/blacklist.conf" || true
    diff_remote_config "$host" "$build_dir/vfio.conf" "/etc/modprobe.d/vfio.conf" || true
    diff_remote_config "$host" "$build_dir/modules" "/etc/modules-load.d/vfio.conf" || true
    diff_remote_config "$host" "$build_dir/cmdline" "/etc/kernel/cmdline" || true

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
    ssh "$host" "cd /tmp/homelab-pve-gpu-passthrough && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -eq 0 ]; then ./scripts/install.sh '$host'; elif command -v sudo >/dev/null 2>&1; then sudo ./scripts/install.sh '$host'; else echo 'Error: current user is not root and sudo is not installed' >&2; exit 1; fi"
}

# --- Main ---
print_sub "WARNING: This will modify systemd-boot cmdline, modules, and initramfs"
deploy_init "GPU Passthrough Configs"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
