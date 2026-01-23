#!/bin/bash
# remove.sh - Remove GPU passthrough configuration from PVE nodes
#
# Usage (from helm):
#   ./remove.sh <hostname|all>
#   ./remove.sh --yes ace
#   ./remove.sh --dry-run ace
#
# Usage (local emergency on Proxmox host):
#   /root/pve-gpu-passthrough-remove.sh [--dry-run]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

SKIP_CONFIRM=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)
            SKIP_CONFIRM=true
            shift
            ;;
        --help|-h)
            cat << 'EOF'
Usage: ./remove.sh [--yes] [--dry-run] <hostname|all>

Options:
  --yes, -y       Skip confirmation prompt
  --dry-run, -n   Preview changes without executing

For local emergency removal on a Proxmox host:
  /root/pve-gpu-passthrough-remove.sh
EOF
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature gpu)"
if ! HOSTS=$(filter_hosts "${1:-}" "${SUPPORTED_HOSTS[@]}"); then
    echo "ERROR: No hostname specified or host not supported"
    echo "Supported hosts: ${SUPPORTED_HOSTS[*]}"
    exit 1
fi

if [[ "$SKIP_CONFIRM" == "false" && "$DRY_RUN" == "false" ]]; then
    print_header "GPU Passthrough Removal Plan"
    echo "Hosts: $HOSTS"
    echo ""
    echo "Actions per host:"
    echo "  - Remove video=efifb:off from kernel cmdline"
    echo "  - Comment out GPU driver blacklists"
    echo "  - Comment out VFIO device bindings"
    echo "  - Remove VFIO module config"
    echo "  - Update initramfs and bootloader"
    echo ""
    read -p "Proceed with removal? [y/N]: " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
    echo ""
fi

remove_gpu_passthrough() {
    local host="$1"
    local dry_run_flag=""

    if [[ "$DRY_RUN" == "true" ]]; then
        dry_run_flag="--dry-run"
    fi

    print_sub "Staging removal script..."
    ssh "$host" "rm -rf /tmp/homelab-pve-gpu-passthrough && mkdir -p /tmp/homelab-pve-gpu-passthrough/lib"
    scp -q "$SCRIPT_DIR/scripts/remove-local.sh" "$host:/tmp/homelab-pve-gpu-passthrough/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-gpu-passthrough/lib/"

    print_sub "Running removal..."
    ssh "$host" "chmod +x /tmp/homelab-pve-gpu-passthrough/remove-local.sh && sudo /tmp/homelab-pve-gpu-passthrough/remove-local.sh $dry_run_flag"

    ssh "$host" "rm -rf /tmp/homelab-pve-gpu-passthrough"
}

deploy_init "GPU Passthrough Removal"
deploy_run remove_gpu_passthrough $HOSTS
deploy_finish

if [[ "$DRY_RUN" == "false" ]]; then
    echo ""
    echo "IMPORTANT: Reboot required to apply changes:"
    for host in $HOSTS; do
        echo "  ssh $host reboot"
    done
fi
