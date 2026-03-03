#!/bin/bash
# install.sh - Install docker management scripts
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub() { echo "    $*"; }
    file_needs_update() {
        local src="$1"
        local dst="$2"
        [[ -f "$src" ]] || return 2
        [[ ! -f "$dst" ]] && return 0
        [[ "$FORCE_UPDATE" == "true" ]] && return 0
        cmp -s "$src" "$dst" && return 1
        return 0
    }
    copy_if_changed() {
        local src="$1"
        local dst="$2"
        local label="${3:-$dst}"
        if file_needs_update "$src" "$dst"; then
            cp "$src" "$dst"
            print_sub "Updated $label"
            return 0
        fi
        local rc=$?
        [[ $rc -eq 1 ]] && { print_sub "$label unchanged; skipping update"; return 1; }
        return "$rc"
    }
fi

if [[ ! -f "$BUILD_DIR/env" ]]; then
    echo "Error: Missing env file at $BUILD_DIR/env"
    exit 1
fi

# shellcheck source=/dev/null
source "$BUILD_DIR/env"

APPDATA_DEST="/mnt/cache/appdata"
APPDATA_SCRIPTS_DIR="${APPDATA_DEST}/scripts"
APPDATA_LOGS_DIR="${APPDATA_SCRIPTS_DIR}/logs"

if [[ -z "$DOCKER_USER" ]]; then
    echo "Error: DOCKER_USER missing"
    exit 1
fi

DOCKER_OWNER="${DOCKER_OWNER:-$DOCKER_USER}"
DOCKER_GROUP="${DOCKER_GROUP:-$DOCKER_OWNER}"

mkdir -p "$APPDATA_DEST"

for script in start.sh rm.sh; do
    if ! copy_if_changed "$SCRIPT_DIR/scripts/$script" "${APPDATA_DEST}/${script}" "$script"; then
        rc=$?
        [[ $rc -eq 1 ]] || exit "$rc"
    fi
    chown "${DOCKER_OWNER}:${DOCKER_GROUP}" "${APPDATA_DEST}/${script}"
    chmod +x "${APPDATA_DEST}/${script}"
done

if [[ "$DOCKER_BACKUP" == "true" ]]; then
    mkdir -p "$APPDATA_SCRIPTS_DIR" "$APPDATA_LOGS_DIR"
    if ! copy_if_changed "$SCRIPT_DIR/scripts/backup.sh" "$APPDATA_SCRIPTS_DIR/backup.sh" "backup.sh"; then
        rc=$?
        [[ $rc -eq 1 ]] || exit "$rc"
    fi
    chown "${DOCKER_OWNER}:${DOCKER_GROUP}" "$APPDATA_SCRIPTS_DIR/backup.sh"
    chmod +x "$APPDATA_SCRIPTS_DIR/backup.sh"
fi
