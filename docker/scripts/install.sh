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

mkdir -p "$APPDATA_DEST"
mkdir -p "$APPDATA_SCRIPTS_DIR"

for script in start.sh rm.sh; do
    rc=0
    copy_if_changed "$SCRIPT_DIR/scripts/$script" "${APPDATA_DEST}/${script}" "$script" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    chmod +x "${APPDATA_DEST}/${script}"
done

rc=0
copy_if_changed "$SCRIPT_DIR/scripts/docker-common.sh" "$APPDATA_SCRIPTS_DIR/docker-common.sh" "docker-common.sh" || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
chmod +x "$APPDATA_SCRIPTS_DIR/docker-common.sh"

if [[ "$DOCKER_BACKUP" == "true" ]]; then
    mkdir -p "$APPDATA_LOGS_DIR"
    rc=0
    copy_if_changed "$SCRIPT_DIR/scripts/backup.sh" "$APPDATA_SCRIPTS_DIR/backup.sh" "backup.sh" || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    chmod +x "$APPDATA_SCRIPTS_DIR/backup.sh"
fi
