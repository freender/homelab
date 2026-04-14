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
APPDATA_LOGS_DIR="${APPDATA_SCRIPTS_DIR}/logs"
HOMELAB_DOCKER_DIR="${APPDATA_DEST}/.homelab/docker"

mkdir -p "$APPDATA_DEST"
mkdir -p "$APPDATA_SCRIPTS_DIR" "$APPDATA_LOGS_DIR" "$HOMELAB_DOCKER_DIR"

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

if [[ "$DOCKER_BACKUP" == "true" ]]; then
    rc=0
    copy_if_changed "$SCRIPT_DIR/scripts/backup.sh" "$HOMELAB_DOCKER_DIR/backup.sh" "backup.sh" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    chmod +x "$HOMELAB_DOCKER_DIR/backup.sh"
fi

# --- systemd timer for scheduled container startup ---

units_changed=false

if [[ "$ENABLE_DOCKER_START_TIMER" == "true" ]]; then
    for unit in homelab-docker-start.service homelab-docker-start.timer; do
        rc=0
        copy_if_changed "$BUILD_DIR/$unit" "/etc/systemd/system/$unit" "$unit" || rc=$?
        [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
        [[ $rc -eq 0 ]] && units_changed=true
    done
fi

if [[ "$ENABLE_SYNCTHING_TIMERS" == "true" ]]; then
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
ensure_timer_state "syncthing-unpause.timer" "$ENABLE_SYNCTHING_TIMERS" "$units_changed"
ensure_timer_state "syncthing-pause.timer" "$ENABLE_SYNCTHING_TIMERS" "$units_changed"
