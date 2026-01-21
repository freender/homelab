#!/bin/bash
# Deploy network interfaces config to PVE/PBS nodes
# Usage: ./deploy.sh [ace|bray|clovis|xur|all]

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

# Get supported hosts from registry (PVE + PBS)
SUPPORTED_HOSTS=($(get_hosts_by_type pve) $(get_hosts_by_type pbs))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-interfaces (not applicable to $1)"
    exit 0
fi

print_action "Deploying Network Interfaces"
print_sub "Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    print_action "Deploying to $host..."

    host_type=$(get_host_type "$host")

    if [[ "$host_type" == "pve" ]]; then
        template_file="$SCRIPT_DIR/templates/pve-interfaces"
    else
        template_file="$SCRIPT_DIR/templates/pbs-interfaces"
    fi

    if [[ ! -f "$template_file" ]]; then
        print_warn "No template found for node type: $host_type"
        print_sub "Expected: $template_file"
        continue
    fi

    mgmt_ip=$(get_host_kv "$host" "net.mgmt_ip")
    if [[ "$host_type" == "pve" ]]; then
        storage_ip=$(get_host_kv "$host" "net.storage_ip" || true)
    else
        storage_ip=""
    fi
    gateway=$(get_host_kv "$host" "net.gateway")

    if [[ -z "$mgmt_ip" || -z "$gateway" ]]; then
        print_warn "Missing net.mgmt_ip or net.gateway for $host in pve-interfaces/hosts.conf"
        continue
    fi

    if [[ "$host_type" == "pve" && -z "$storage_ip" ]]; then
        print_warn "Missing net.storage_ip for $host in pve-interfaces/hosts.conf"
        continue
    fi

    content=$(cat "$template_file")
    content=${content//\$\{NET_MGMT_IP\}/$mgmt_ip}
    content=${content//\$\{NET_GATEWAY\}/$gateway}
    if [[ -n "$storage_ip" ]]; then
        content=${content//\$\{NET_STORAGE_IP\}/$storage_ip}
    fi

    rendered_file=$(mktemp)
    printf '%s\n' "$content" > "$rendered_file"

    # Backup existing config
    print_sub "Backing up existing config..."
    ssh "$host" "cp /etc/network/interfaces /etc/network/interfaces.bak.\$(date +%Y%m%d%H%M%S)"

    # Copy new config
    print_sub "Copying interfaces file..."
    deploy_file "$rendered_file" "$host" "/etc/network/interfaces"

    rm -f "$rendered_file"

    print_ok "Deployed to $host (reboot or ifreload required)"
    echo ""
done

print_action "Deployment complete!"
echo ""
echo "Apply changes:"
echo "  ssh <node> ifreload -a   # Apply without reboot (may disrupt connections)"
echo "  ssh <node> reboot        # Or reboot to apply safely"
