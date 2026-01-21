#!/bin/bash
# Deploy SSH config to homelab infrastructure
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="${SCRIPT_DIR}/configs"
COMMON_CONFIG="${CONFIGS_DIR}/common.conf"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

# Get all hosts from registry
SUPPORTED_HOSTS=($(get_all_hosts))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping ssh (not applicable to $1)"
    exit 0
fi

if [[ ! -f "$COMMON_CONFIG" ]]; then
    echo "Error: common.conf not found at $COMMON_CONFIG"
    exit 1
fi

deploy_to_host() {
    local host="$1"
    print_action "Deploying to $host..."
    
    # Ensure .ssh directory exists
    if ! ssh "$host" "mkdir -p ~/.ssh && chmod 700 ~/.ssh" 2>/dev/null; then
        print_warn "Failed to create .ssh directory on $host"
        return 1
    fi
    
    # Start with common config
    print_sub "Deploying base config..."
    scp -q "$COMMON_CONFIG" "${host}:/tmp/ssh_config"
    
    # Append host-specific config if exists
    if [[ -f "$CONFIGS_DIR/$host/append.conf" ]]; then
        print_sub "Appending $host-specific config..."
        cat "$CONFIGS_DIR/$host/append.conf" | ssh "$host" "cat >> /tmp/ssh_config"
    fi
    
    # Move to final location
    ssh "$host" "mv /tmp/ssh_config ~/.ssh/config && chmod 600 ~/.ssh/config"
    
    # Verify deployment
    if ssh "$host" "test -f ~/.ssh/config && test -r ~/.ssh/config" 2>/dev/null; then
        print_ok "Deployment successful"
    else
        print_warn "Deployment verification failed"
        return 1
    fi
    
    echo ""
}

print_action "Deploying SSH Config"
print_sub "Source: $COMMON_CONFIG"
print_sub "Hosts: $HOSTS"
echo ""

FAILED_HOSTS=()

for host in $HOSTS; do
    if ! deploy_to_host "$host"; then
        FAILED_HOSTS+=("$host")
    fi
done

print_action "Deployment complete!"

if [[ ${#FAILED_HOSTS[@]} -gt 0 ]]; then
    echo ""
    echo "Failed hosts: ${FAILED_HOSTS[*]}"
    exit 1
fi
