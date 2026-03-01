#!/bin/bash
# Deploy PVE cluster config backup workflow
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
CONFIGS_DIR="$SCRIPT_DIR/configs"
PBS_ENV_SOURCE="$CONFIGS_DIR/pbs.env"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-config-backup)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-config-backup (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type
    local schedule
    local repository
    local backup_id
    local archive_name
    local build_dir="$BUILD_ROOT/$host"

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    if [[ "$host_type" != "pve" ]]; then
        print_warn "Skipping $host: pve-config-backup supports type pve only"
        return 0
    fi

    schedule=$(hosts get "$host" "pve-config-backup.schedule" "00:30")
    repository=$(hosts get "$host" "pve-config-backup.repository") || { print_warn "pve-config-backup.repository missing for $host"; return 1; }
    backup_id=$(hosts get "$host" "pve-config-backup.backup_id" "pve-cluster-config")
    archive_name=$(hosts get "$host" "pve-config-backup.archive_name" "etc-pve")

    if [[ ! -f "$PBS_ENV_SOURCE" ]]; then
        print_warn "Missing secret file: $PBS_ENV_SOURCE"
        print_warn "Create it from: $CONFIGS_DIR/pbs.env.example"
        return 1
    fi

    prepare_build_dir "$build_dir"

    render_template "$CONFIGS_DIR/pve-config-backup.sh.tpl" "$build_dir/pve-config-backup.sh" \
        REPOSITORY="$repository" \
        BACKUP_ID="$backup_id" \
        ARCHIVE_NAME="$archive_name"
    chmod 700 "$build_dir/pve-config-backup.sh"

    cat > "$build_dir/pve-config-backup.service" <<EOF
[Unit]
Description=PVE cluster config backup to PBS
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/homelab/pve-config-backup.env
ExecStart=/root/pve-config-backup.sh
EOF

    cat > "$build_dir/pve-config-backup.timer" <<EOF
[Unit]
Description=Daily PVE cluster config backup

[Timer]
OnCalendar=*-*-* $schedule
RandomizedDelaySec=120
Persistent=true
Unit=pve-config-backup.service

[Install]
WantedBy=timers.target
EOF

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-config-backup/"
        print_sub "Repository: $repository"
        print_sub "Backup ID: $backup_id"
        print_sub "Archive: ${archive_name}.pxar:/etc/pve"
        print_sub "Schedule: $schedule"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-config-backup && mkdir -p /tmp/homelab-pve-config-backup/build /tmp/homelab-pve-config-backup/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-config-backup/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-config-backup/"
    scp -q "$PBS_ENV_SOURCE" "$host:/tmp/homelab-pve-config-backup/build/$host/pbs.env"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-config-backup/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-config-backup && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh '$host'"
}

deploy_init "PVE Config Backup"
deploy_run deploy $HOSTS
deploy_finish
