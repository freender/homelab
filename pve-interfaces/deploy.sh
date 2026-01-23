#!/bin/bash
# Deploy network interfaces config to PVE/PBS nodes
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

SUPPORTED_HOSTS=($(hosts list --type pve) $(hosts list --type pbs))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-interfaces (not applicable to $1)"
    exit 0
fi

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local host_type template_file mgmt_ip storage_ip gateway
    local build_dir="$BUILD_ROOT/$host"

    host_type=$(hosts get "$host" "type")

    if [[ "$host_type" == "pve" ]]; then
        template_file="$SCRIPT_DIR/templates/pve-interfaces"
    else
        template_file="$SCRIPT_DIR/templates/pbs-interfaces"
    fi

    [[ ! -f "$template_file" ]] && { print_warn "No template for type: $host_type"; return 1; }

    mgmt_ip=$(hosts get "$host" "net.mgmt_ip") || { print_warn "net.mgmt_ip missing"; return 1; }
    gateway=$(hosts get "$host" "net.gateway") || { print_warn "net.gateway missing"; return 1; }

    if [[ "$host_type" == "pve" ]]; then
        storage_ip=$(hosts get "$host" "net.storage_ip" "") || true
        [[ -z "$storage_ip" ]] && { print_warn "net.storage_ip missing"; return 1; }
    fi

    prepare_build_dir "$build_dir"

    if [[ "$host_type" == "pve" ]]; then
        render_template "$template_file" "$build_dir/interfaces" \
            NET_MGMT_IP="$mgmt_ip" NET_GATEWAY="$gateway" NET_STORAGE_IP="$storage_ip"
    else
        render_template "$template_file" "$build_dir/interfaces" \
            NET_MGMT_IP="$mgmt_ip" NET_GATEWAY="$gateway"
    fi

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-interfaces/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-interfaces && mkdir -p /tmp/homelab-pve-interfaces/build /tmp/homelab-pve-interfaces/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-interfaces/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-interfaces/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-interfaces/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-interfaces && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "Network Interfaces"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Apply changes:"
echo "  ssh <node> ifreload -a   # Apply without reboot (may disrupt connections)"
echo "  ssh <node> reboot        # Or reboot to apply safely"
