#!/bin/bash
# Deploy docker management scripts
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
SUPPORTED_HOSTS=($(hosts list --feature docker))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping docker (not applicable to $1)"
    exit 0
fi

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"
    local user backup_enabled no_cron

    user=$(hosts get "$host" "docker.user") || { print_warn "docker.user missing"; return 1; }
    backup_enabled=false
    no_cron=false
    hosts has "$host" "docker-backup" && backup_enabled=true
    hosts has "$host" "docker-no-cron" && no_cron=true

    rm -rf "$build_dir"
    mkdir -p "$build_dir"

    cat > "$build_dir/env" <<EOF
DOCKER_USER="$user"
DOCKER_BACKUP="$backup_enabled"
DOCKER_NO_CRON="$no_cron"
EOF

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-docker && mkdir -p /tmp/homelab-docker/build"
    scp -rq "$build_dir" "$host:/tmp/homelab-docker/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-docker/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-docker && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "Docker Management Scripts"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Manual usage:"
echo "  cd /mnt/cache/appdata && ./start.sh   # Update and start containers"
echo "  cd /mnt/cache/appdata && ./rm.sh      # Stop all containers"
