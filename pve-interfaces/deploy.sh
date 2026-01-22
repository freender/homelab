#!/bin/bash
# Deploy network interfaces config to PVE/PBS nodes
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"

# --- Host Selection ---
SUPPORTED_HOSTS=($(hosts list --type pve) $(hosts list --type pbs))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-interfaces (not applicable to $1)"
    exit 0
fi

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local host_type template_file mgmt_ip storage_ip gateway
    
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
    
    # Render template
    local content
    content=$(cat "$template_file")
    content=${content//\$\{NET_MGMT_IP\}/$mgmt_ip}
    content=${content//\$\{NET_GATEWAY\}/$gateway}
    [[ -n "$storage_ip" ]] && content=${content//\$\{NET_STORAGE_IP\}/$storage_ip}
    
    local rendered_file
    rendered_file=$(mktemp)
    printf '%s\n' "$content" > "$rendered_file"
    
    print_sub "Backing up existing config..."
    ssh "$host" "cp /etc/network/interfaces /etc/network/interfaces.bak.\$(date +%Y%m%d%H%M%S)"
    
    print_sub "Deploying interfaces file..."
    deploy_file "$rendered_file" "$host" "/etc/network/interfaces"
    rm -f "$rendered_file"
    
    print_sub "Reboot or ifreload required"
}

# --- Main ---
deploy_init "Network Interfaces"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Apply changes:"
echo "  ssh <node> ifreload -a   # Apply without reboot (may disrupt connections)"
echo "  ssh <node> reboot        # Or reboot to apply safely"
