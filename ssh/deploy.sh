#!/bin/bash
# Deploy SSH config to homelab infrastructure
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
CONFIGS_DIR="$SCRIPT_DIR/configs"
COMMON_CONFIG="$CONFIGS_DIR/common.conf"
BUILD_ROOT="$SCRIPT_DIR/build"

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
    local build_dir="$BUILD_ROOT/$host"

    rm -rf "$build_dir"
    mkdir -p "$build_dir"

    cp "$COMMON_CONFIG" "$build_dir/config"
    if [[ -f "$CONFIGS_DIR/$host/append.conf" ]]; then
        cat "$CONFIGS_DIR/$host/append.conf" >> "$build_dir/config"
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-ssh && mkdir -p /tmp/homelab-ssh/build"
    scp -rq "$build_dir" "$host:/tmp/homelab-ssh/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-ssh/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-ssh && chmod +x scripts/install.sh && ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "SSH Config"
deploy_run deploy $HOSTS
deploy_finish
