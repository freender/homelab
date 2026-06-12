#!/bin/bash
# install.sh - Install PBS client backup script and timer

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1
load_file_map

print_header "PBS Client Backup"

for required in \
    homelab-pbs-client-backup \
    homelab-pbs-client-backup.conf \
    homelab-pbs-client-backup.env \
    homelab-pbs-client-backup.service \
    homelab-pbs-client-backup.timer; do
    require_file "$BUILD_DIR/$required" "$BUILD_DIR/$required" || exit 1
done

# shellcheck source=/dev/null
source "$BUILD_DIR/homelab-pbs-client-backup.conf"

case "${RUNNER:-host}" in
    host|native)
        if ! command -v proxmox-backup-client >/dev/null 2>&1; then
            print_error "proxmox-backup-client not found"
            exit 1
        fi
        ;;
    docker)
        if ! command -v docker >/dev/null 2>&1; then
            print_error "docker not found"
            exit 1
        fi
        ;;
    *)
        print_error "Unsupported RUNNER: ${RUNNER:-}"
        exit 1
        ;;
esac

archive_count="${ARCHIVE_COUNT:-0}"
if [[ "$archive_count" =~ ^[0-9]+$ ]]; then
    for ((i = 0; i < archive_count; i++)); do
        dataset_var="ARCHIVE_${i}_DATASET"
        if [[ -n "${!dataset_var:-}" ]] && ! command -v zfs >/dev/null 2>&1; then
            print_error "zfs command not found"
            exit 1
        fi
    done
fi

changed=false
rc=0
install_file_map || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
[[ $rc -eq 0 ]] && changed=true

if [[ "$changed" == true ]]; then
    systemctl daemon-reload
fi

if [[ "${RETIRE_PVE_CONFIG_BACKUP:-false}" == "true" ]]; then
    retired_changed=false
    for timer in pve-config-backup.timer homelab-pve-config-backup.timer; do
        if systemctl is-enabled --quiet "$timer" 2>/dev/null; then
            systemctl disable --now "$timer" >/dev/null
            print_sub "Retired $timer disabled"
            retired_changed=true
        elif systemctl is-active --quiet "$timer" 2>/dev/null; then
            systemctl stop "$timer" >/dev/null
            print_sub "Retired $timer stopped"
            retired_changed=true
        fi
    done
    for retired_file in \
        /root/pve-config-backup.sh \
        /etc/homelab/pve-config-backup.env \
        /etc/systemd/system/pve-config-backup.service \
        /etc/systemd/system/pve-config-backup.timer \
        /etc/systemd/system/homelab-pve-config-backup.service \
        /etc/systemd/system/homelab-pve-config-backup.timer; do
        if [[ -e "$retired_file" ]]; then
            rm -f "$retired_file"
            retired_changed=true
        fi
    done
    if [[ "$retired_changed" == true ]]; then
        systemctl daemon-reload
    fi
fi

systemctl enable --now homelab-pbs-client-backup.timer >/dev/null
print_ok "homelab-pbs-client-backup.timer enabled"

systemctl list-timers homelab-pbs-client-backup.timer --no-pager --all || true
print_ok "PBS client backup installed"
