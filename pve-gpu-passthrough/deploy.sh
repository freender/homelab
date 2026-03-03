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
REQUIRED_ROOT_TOKEN="root=ZFS=rpool/ROOT/pve-1"
ROOT_DATASET=""

# --- Host Selection ---
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-gpu-passthrough)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-gpu-passthrough (not applicable to $1)"
    exit 0
fi

# --- Validation ---
[[ ! -f "$MODULES_FILE" ]] && { print_warn "modules file not found: $MODULES_FILE"; exit 1; }
[[ ! -f "$BLACKLIST_FILE" ]] && { print_warn "blacklist file not found: $BLACKLIST_FILE"; exit 1; }
[[ ! -f "$CMDLINE_FILE" ]] && { print_warn "cmdline file not found: $CMDLINE_FILE"; exit 1; }
[[ ! -f "$VFIO_TEMPLATE" ]] && { print_warn "vfio template not found: $VFIO_TEMPLATE"; exit 1; }

cmdline_value=$(head -n 1 "$CMDLINE_FILE")
if [[ "$cmdline_value" != *"$REQUIRED_ROOT_TOKEN"* ]]; then
    print_warn "Unsafe cmdline in $CMDLINE_FILE"
    print_warn "Missing required token: $REQUIRED_ROOT_TOKEN"
    print_warn "Refusing deploy to avoid boot breakage"
    exit 1
fi
ROOT_DATASET="${REQUIRED_ROOT_TOKEN#root=ZFS=}"

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local pci_ids
    local build_dir="$BUILD_ROOT/$host"

    pci_ids=$(hosts get "$host" "pve-gpu-passthrough.pci_ids") || { print_warn "pve-gpu-passthrough.pci_ids missing"; return 1; }

    if ! ssh "$host" "zfs list -H -o name '$ROOT_DATASET' >/dev/null 2>&1"; then
        print_warn "Required ZFS dataset not found on $host: $ROOT_DATASET"
        print_warn "Refusing deploy to avoid boot breakage"
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
    ssh "$host" "cd /tmp/homelab-pve-gpu-passthrough && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE/PBS deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh '$host'"
}

# --- Main ---
print_sub "WARNING: This will modify systemd-boot cmdline, modules, and initramfs"
deploy_init "GPU Passthrough Configs"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "IMPORTANT: Reboot nodes to apply GPU passthrough changes:"
echo "  ssh <node> reboot"
