#!/bin/bash
# install.sh - Install docker management scripts
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub() { echo "    $*"; }
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
    cp "$SCRIPT_DIR/scripts/$script" "${APPDATA_DEST}/${script}"
    chown "${DOCKER_OWNER}:${DOCKER_GROUP}" "${APPDATA_DEST}/${script}"
    chmod +x "${APPDATA_DEST}/${script}"
done

if [[ "$DOCKER_BACKUP" == "true" ]]; then
    mkdir -p "$APPDATA_SCRIPTS_DIR" "$APPDATA_LOGS_DIR"
    cp "$SCRIPT_DIR/scripts/backup.sh" "$APPDATA_SCRIPTS_DIR/backup.sh"
    chown "${DOCKER_OWNER}:${DOCKER_GROUP}" "$APPDATA_SCRIPTS_DIR/backup.sh"
    chmod +x "$APPDATA_SCRIPTS_DIR/backup.sh"
fi
