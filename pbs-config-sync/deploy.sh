#!/bin/bash
# Deploy PBS config sync script and timer to PBS hosts
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
CONFIGS_DIR="$SCRIPT_DIR/configs"
TELEGRAM_ENV_SOURCE="$HOMELAB_ROOT/apcupsd/configs/telegram/telegram.env"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pbs-config-sync)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pbs-config-sync (not applicable to $1)"
    exit 0
fi

if [[ ! -f "$TELEGRAM_ENV_SOURCE" ]]; then
    echo "ERROR: telegram env not found: $TELEGRAM_ENV_SOURCE"
    echo "Expected secret file from apcupsd module to exist."
    exit 1
fi

deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"
    local dest_host
    local dest_user
    local dest_path
    local schedule

    dest_user=$(hosts get "$host" "pbs-config-sync.dest_user" "freender")
    dest_host=$(hosts get "$host" "pbs-config-sync.dest_host") || { print_warn "pbs-config-sync.dest_host missing for $host"; return 1; }
    dest_path=$(hosts get "$host" "pbs-config-sync.dest_path") || { print_warn "pbs-config-sync.dest_path missing for $host"; return 1; }
    schedule=$(hosts get "$host" "pbs-config-sync.schedule" "00:30")

    prepare_build_dir "$build_dir"

    render_template "$CONFIGS_DIR/backup-config.sh.tpl" "$build_dir/backup-config.sh" \
        DEST_USER="$dest_user" \
        DEST_HOST="$dest_host" \
        DEST_PATH="$dest_path"
    chmod 700 "$build_dir/backup-config.sh"

    cat > "$build_dir/backup-config.service" <<EOF
[Unit]
Description=PBS config backup to ${dest_user}@${dest_host}
After=network-online.target
Wants=network-online.target
OnFailure=backup-config-notify@%n.service

[Service]
Type=oneshot
ExecStart=/root/backup-config.sh
StandardOutput=journal
StandardError=journal
EOF

    cat > "$build_dir/backup-config-notify@.service" <<EOF
[Unit]
Description=Notify on %i failure

[Service]
Type=oneshot
ExecStart=/bin/bash -lc '/etc/apcupsd/telegram/telegram.sh -s "Config Backup FAILED" -d "\$(hostname) PBS config sync to ${dest_user}@${dest_host}:${dest_path} failed. Check: journalctl -u backup-config"'
EOF

    cat > "$build_dir/backup-config.timer" <<EOF
[Unit]
Description=Daily PBS config backup
After=network-online.target

[Timer]
OnCalendar=*-*-* ${schedule}:00
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
EOF

    print_sub "Comparing with remote configs..."
    diff_remote_config "$host" "$build_dir/backup-config.sh" "/root/backup-config.sh" || true
    diff_remote_config "$host" "$build_dir/backup-config.service" "/etc/systemd/system/backup-config.service" || true
    diff_remote_config "$host" "$build_dir/backup-config-notify@.service" "/etc/systemd/system/backup-config-notify@.service" || true
    diff_remote_config "$host" "$build_dir/backup-config.timer" "/etc/systemd/system/backup-config.timer" || true

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pbs-config-sync/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pbs-config-sync && mkdir -p /tmp/homelab-pbs-config-sync/build /tmp/homelab-pbs-config-sync/lib /tmp/homelab-pbs-config-sync/configs/telegram"
    scp -rq "$build_dir" "$host:/tmp/homelab-pbs-config-sync/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pbs-config-sync/"
    scp -q "$TELEGRAM_ENV_SOURCE" "$host:/tmp/homelab-pbs-config-sync/configs/telegram/telegram.env"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pbs-config-sync/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pbs-config-sync && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PBS config sync deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh '$host' '$schedule'"
}

deploy_init "PBS Config Sync"
deploy_run deploy $HOSTS
deploy_finish
