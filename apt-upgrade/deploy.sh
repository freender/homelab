#!/bin/bash
# Deploy apt dist-upgrade
# Usage: ./deploy.sh [--cleanup] [--dry-run] [host|all]
#
# Behaviour per host:
#   apt-upgrade:               on-demand only (running this script upgrades the host)
#   apt-upgrade.autoupgrade:   also installs a daily systemd timer at apt-upgrade.schedule

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
CLEANUP=false

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

# Parse module-specific flags
REMAINING_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cleanup)
            CLEANUP=true
            shift
            ;;
        *)
            REMAINING_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${REMAINING_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature apt-upgrade)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apt-upgrade (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type autoupgrade schedule

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    case "$host_type" in
        pve|ubuntu)
            ;;
        *)
            print_warn "Skipping $host: apt-upgrade supports type pve/ubuntu only"
            return 0
            ;;
    esac

    # autoupgrade subfeature: install persistent daily timer
    autoupgrade=$(hosts get "$host" "apt-upgrade.autoupgrade" "false") || autoupgrade="false"
    schedule=$(hosts get "$host" "apt-upgrade.schedule" "09:00") || schedule="09:00"

    local build_dir="$BUILD_ROOT/$host"
    prepare_build_dir "$build_dir"

    # Service unit (always rendered; used both by on-demand deploy and autoupgrade timer)
    cat > "$build_dir/service" <<EOF
[Unit]
Description=Homelab daily apt update and dist-upgrade
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/apt-get update
ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y dist-upgrade
EOF

    if [[ "$CLEANUP" == true ]]; then
        cat >> "$build_dir/service" <<EOF
ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y autoremove
ExecStart=/usr/bin/apt-get -y autoclean
EOF
    fi

    # Timer unit (only rendered when autoupgrade is enabled)
    if [[ "$autoupgrade" == "true" ]]; then
        cat > "$build_dir/timer" <<EOF
[Unit]
Description=Run homelab daily apt update and dist-upgrade

[Timer]
OnCalendar=*-*-* ${schedule}:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
    fi

    cat > "$build_dir/env" <<EOF
CLEANUP="$CLEANUP"
AUTOUPGRADE="$autoupgrade"
SCHEDULE="$schedule"
EOF

    # Show remote diffs
    diff_remote_config "$host" "$build_dir/service" "/etc/systemd/system/homelab-apt-dist-upgrade.service" || true
    if [[ "$autoupgrade" == "true" ]]; then
        diff_remote_config "$host" "$build_dir/timer" "/etc/systemd/system/homelab-apt-dist-upgrade.timer" || true
    fi

    if [[ "$DRY_RUN" == true ]]; then
        if [[ "$autoupgrade" == "true" ]]; then
            print_sub "[DRY-RUN] Would install daily apt dist-upgrade timer on $host at $schedule"
        else
            print_sub "[DRY-RUN] Would run apt dist-upgrade on $host (on-demand only)"
        fi
        [[ "$CLEANUP" == true ]] && print_sub "[DRY-RUN] Would enable cleanup (autoremove, autoclean)"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-apt-upgrade && mkdir -p /tmp/homelab-apt-upgrade/build /tmp/homelab-apt-upgrade/lib /tmp/homelab-apt-upgrade/scripts"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-apt-upgrade/"
    if [[ "$autoupgrade" == "true" ]]; then
        scp -q "$build_dir/service" "$build_dir/timer" "$build_dir/env" "$host:/tmp/homelab-apt-upgrade/build/"
    else
        scp -q "$build_dir/service" "$build_dir/env" "$host:/tmp/homelab-apt-upgrade/build/"
    fi
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-apt-upgrade/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-apt-upgrade && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: deploy requires root SSH user' >&2; exit 1; fi && FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh"
}

deploy_init "APT Dist-Upgrade"
deploy_run deploy $HOSTS
deploy_finish
