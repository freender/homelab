#!/bin/bash
# Deploy docker management scripts
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature docker)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping docker (not applicable to $1)"
    exit 0
fi

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"
    local user owner group backup_enabled

    user=$(hosts get "$host" "docker.user") || { print_warn "docker.user missing"; return 1; }
    owner=$(hosts get "$host" "docker.owner" "$user")
    group=$(hosts get "$host" "docker.group" "$owner")
    backup_enabled=$(hosts get "$host" "docker.backup" "false")

    prepare_build_dir "$build_dir"

    cat > "$build_dir/env" <<EOF
DOCKER_USER="$user"
DOCKER_OWNER="$owner"
DOCKER_GROUP="$group"
DOCKER_BACKUP="$backup_enabled"
EOF

    print_sub "Comparing with remote scripts..."
    diff_remote_config "$host" "$SCRIPT_DIR/scripts/start.sh" "/mnt/cache/appdata/start.sh" || true
    diff_remote_config "$host" "$SCRIPT_DIR/scripts/rm.sh" "/mnt/cache/appdata/rm.sh" || true
    diff_remote_config "$host" "$SCRIPT_DIR/scripts/docker-common.sh" "/mnt/cache/appdata/scripts/docker-common.sh" || true
    if [[ "$backup_enabled" == "true" ]]; then
        diff_remote_config "$host" "$SCRIPT_DIR/scripts/backup.sh" "/mnt/cache/appdata/scripts/backup.sh" || true
    fi

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-docker/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-docker && mkdir -p /tmp/homelab-docker/build /tmp/homelab-docker/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-docker/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-docker/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-docker/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-docker && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -eq 0 ]; then FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh '$host'; elif command -v sudo >/dev/null 2>&1; then sudo FORCE_UPDATE='$FORCE_UPDATE' /tmp/homelab-docker/scripts/install.sh '$host'; else echo 'Error: current user is not root and sudo is not installed' >&2; exit 1; fi"
}

# --- Main ---
deploy_init "Docker Management Scripts"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Manual usage:"
echo "  cd /mnt/cache/appdata && ./start.sh   # Update and start containers"
echo "  cd /mnt/cache/appdata && ./rm.sh      # Stop all containers"
