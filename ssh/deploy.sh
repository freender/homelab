#!/bin/bash
# Deploy SSH config to homelab infrastructure
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
CONFIGS_DIR="${SCRIPT_DIR}/configs"
COMMON_CONFIG="${CONFIGS_DIR}/common.conf"

# --- Host Selection ---
SUPPORTED_HOSTS=($(hosts list))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping ssh (not applicable to $1)"
    exit 0
fi

# --- Validation ---
[[ ! -f "$COMMON_CONFIG" ]] && { echo "Error: $COMMON_CONFIG not found"; exit 1; }

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    
    # Ensure .ssh directory exists
    if ! ssh "$host" "mkdir -p ~/.ssh && chmod 700 ~/.ssh" 2>/dev/null; then
        print_warn "Failed to create .ssh directory"
        return 1
    fi
    
    print_sub "Deploying base config..."
    scp -q "$COMMON_CONFIG" "${host}:/tmp/ssh_config"
    
    # Append host-specific config if exists
    if [[ -f "$CONFIGS_DIR/$host/append.conf" ]]; then
        print_sub "Appending $host-specific config..."
        cat "$CONFIGS_DIR/$host/append.conf" | ssh "$host" "cat >> /tmp/ssh_config"
    fi
    
    ssh "$host" "mv /tmp/ssh_config ~/.ssh/config && chmod 600 ~/.ssh/config"
    
    # Verify
    ssh "$host" "test -f ~/.ssh/config && test -r ~/.ssh/config" 2>/dev/null
}

# --- Main ---
deploy_init "SSH Config"
deploy_run deploy $HOSTS
deploy_finish
