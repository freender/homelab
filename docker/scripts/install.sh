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

APPDATA_DEST="/mnt/cache/appdata"
APPDATA_SCRIPTS_DIR="${APPDATA_DEST}/scripts"
HOMELAB_DOCKER_DIR="${APPDATA_DEST}/.homelab/docker"
SYNCTHING_SCHEDULE_SCRIPT="${APPDATA_SCRIPTS_DIR}/syncthing-schedule.sh"

mkdir -p "$APPDATA_DEST"
mkdir -p "$APPDATA_SCRIPTS_DIR" "$HOMELAB_DOCKER_DIR"

for script in start.sh rm.sh; do
    rc=0
    copy_if_changed "$SCRIPT_DIR/scripts/$script" "${APPDATA_DEST}/${script}" "$script" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    chmod +x "${APPDATA_DEST}/${script}"
done

rc=0
copy_if_changed "$SCRIPT_DIR/scripts/docker-common.sh" "$HOMELAB_DOCKER_DIR/docker-common.sh" "docker-common.sh" || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
chmod +x "$HOMELAB_DOCKER_DIR/docker-common.sh"

# --- systemd timer for scheduled container startup ---

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

if [[ "$RUN_DOCKER_START_ON_BOOT" == "true" || "$ENABLE_DOCKER_START_TIMER" == "true" ]]; then
    rc=0
    copy_if_changed "$BUILD_DIR/homelab-docker-start.service" "/etc/systemd/system/homelab-docker-start.service" "homelab-docker-start.service" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && units_changed=true
fi

if [[ "$ENABLE_DOCKER_START_TIMER" == "true" ]]; then
    rc=0
    copy_if_changed "$BUILD_DIR/homelab-docker-start.timer" "/etc/systemd/system/homelab-docker-start.timer" "homelab-docker-start.timer" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && units_changed=true
fi

if [[ "$ENABLE_DOCKER_UPDATE_TIMER" == "true" ]]; then
    for unit in homelab-docker-update.service homelab-docker-update.timer; do
        rc=0
        copy_if_changed "$BUILD_DIR/$unit" "/etc/systemd/system/$unit" "$unit" || rc=$?
        [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
        [[ $rc -eq 0 ]] && units_changed=true
    done
fi

if [[ "$ENABLE_SYNCTHING_TIMERS" == "true" ]]; then
    if [[ -f "$SYNCTHING_SCHEDULE_SCRIPT" ]]; then
        chmod +x "$SYNCTHING_SCHEDULE_SCRIPT"
    else
        print_warn "Missing $SYNCTHING_SCHEDULE_SCRIPT; Syncthing timers will fail until it is restored"
    fi

    for unit in syncthing-unpause.service syncthing-unpause.timer syncthing-pause.service syncthing-pause.timer; do
        rc=0
        copy_if_changed "$BUILD_DIR/$unit" "/etc/systemd/system/$unit" "$unit" || rc=$?
        [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
        [[ $rc -eq 0 ]] && units_changed=true
    done
fi

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

if [[ "$RUN_DOCKER_START_ON_BOOT" == "true" ]]; then
    if ! systemctl is-enabled --quiet homelab-docker-start.service 2>/dev/null; then
        systemctl enable homelab-docker-start.service
        print_ok "homelab-docker-start.service enabled"
    elif [[ "$units_changed" == "true" ]]; then
        systemctl reenable homelab-docker-start.service
        print_ok "homelab-docker-start.service reenabled"
    else
        print_sub "homelab-docker-start.service already enabled"
    fi
else
    if systemctl is-enabled --quiet homelab-docker-start.service 2>/dev/null; then
        systemctl disable homelab-docker-start.service
        print_ok "homelab-docker-start.service disabled"
    else
        print_sub "homelab-docker-start.service disabled by config"
    fi
fi

ensure_timer_state "homelab-docker-start.timer" "$ENABLE_DOCKER_START_TIMER" "$units_changed"
ensure_timer_state "homelab-docker-update.timer" "$ENABLE_DOCKER_UPDATE_TIMER" "$units_changed"
ensure_timer_state "syncthing-unpause.timer" "$ENABLE_SYNCTHING_TIMERS" "$units_changed"
ensure_timer_state "syncthing-pause.timer" "$ENABLE_SYNCTHING_TIMERS" "$units_changed"
