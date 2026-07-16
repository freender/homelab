#!/bin/bash
# install.sh - Install docker management scripts
# Usage: ./scripts/install.sh [hostname]

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

require_file "$BUILD_DIR/env" "$BUILD_DIR/env" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/env"

# An empty flag reads as "disable the update timer" rather than erroring; refuse to
# run on a truncated env file instead of silently turning the timer off.
require_env ENABLE_DOCKER_UPDATE_TIMER || exit 1

APPDATA_DEST="/mnt/cache/appdata"
APPDATA_SCRIPTS_DIR="${APPDATA_DEST}/scripts"
HOMELAB_DOCKER_DIR="${APPDATA_DEST}/.homelab/docker"

mkdir -p "$APPDATA_DEST"
mkdir -p "$APPDATA_SCRIPTS_DIR" "$HOMELAB_DOCKER_DIR"

for script in start.sh rm.sh rebuild.sh; do
    rc=0
    copy_if_changed "$SCRIPT_DIR/scripts/$script" "${APPDATA_DEST}/${script}" "$script" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    chmod +x "${APPDATA_DEST}/${script}"
done

rc=0
copy_if_changed "$SCRIPT_DIR/scripts/docker-common.sh" "$HOMELAB_DOCKER_DIR/docker-common.sh" "docker-common.sh" || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
chmod +x "$HOMELAB_DOCKER_DIR/docker-common.sh"

rc=0
copy_if_changed "$BUILD_DIR/env" "$HOMELAB_DOCKER_DIR/env" "docker env" || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
chmod 0644 "$HOMELAB_DOCKER_DIR/env"

units_changed=false

cleanup_legacy_backup_unit() {
    local unit="$1"
    local unit_path="/etc/systemd/system/$unit"

    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        systemctl disable --now "$unit"
        print_ok "$unit disabled"
    elif systemctl is-active --quiet "$unit" 2>/dev/null; then
        systemctl stop "$unit"
        print_ok "$unit stopped"
    fi

    if [[ -e "$unit_path" ]]; then
        rm -f "$unit_path"
        units_changed=true
        print_ok "$unit removed"
    fi
}

cleanup_legacy_backup_unit "homelab-docker-backup.timer"
cleanup_legacy_backup_unit "homelab-docker-backup.service"
if [[ -e "$APPDATA_SCRIPTS_DIR/backup.sh" ]]; then
    rm -f "$APPDATA_SCRIPTS_DIR/backup.sh"
    print_ok "backup.sh removed"
fi

cleanup_legacy_backup_unit "homelab-docker-start.timer"
cleanup_legacy_backup_unit "homelab-docker-start.service"
cleanup_legacy_backup_unit "homelab-docker-clean-shutdown.service"
if [[ -e /usr/local/sbin/homelab-docker-clean-shutdown ]]; then
    rm -f /usr/local/sbin/homelab-docker-clean-shutdown
    print_ok "homelab-docker-clean-shutdown removed"
fi
cleanup_legacy_backup_unit "syncthing-unpause.timer"
cleanup_legacy_backup_unit "syncthing-unpause.service"
cleanup_legacy_backup_unit "syncthing-pause.timer"
cleanup_legacy_backup_unit "syncthing-pause.service"

if [[ "$ENABLE_DOCKER_UPDATE_TIMER" == "true" ]]; then
    for unit in homelab-docker-update.service homelab-docker-update.timer; do
        rc=0
        copy_if_changed "$BUILD_DIR/$unit" "/etc/systemd/system/$unit" "$unit" || rc=$?
        [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
        [[ $rc -eq 0 ]] && units_changed=true
    done
fi

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

ensure_timer_state "homelab-docker-update.timer" "$ENABLE_DOCKER_UPDATE_TIMER" "$units_changed"
