#!/bin/bash

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1
require_file "$BUILD_DIR/media-mover.env" "$BUILD_DIR/media-mover.env" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/media-mover.env"

load_file_map() {
    local map_file="$BUILD_DIR/file-map.conf"
    local filename remote_path mode

    declare -g -A FILE_MAP_DEST=()
    declare -g -A FILE_MAP_MODE=()
    while IFS='|' read -r filename remote_path mode; do
        FILE_MAP_DEST["$filename"]="$remote_path"
        FILE_MAP_MODE["$filename"]="${mode:-644}"
    done < "$map_file"
}

mapped_dest() {
    printf '%s\n' "${FILE_MAP_DEST[$1]}"
}

mapped_mode() {
    printf '%s\n' "${FILE_MAP_MODE[$1]:-644}"
}

install_build_file() {
    local name="$1"
    local rc=0

    install_if_changed "$BUILD_DIR/$name" "$(mapped_dest "$name")" "$(mapped_mode "$name")" "$(mapped_dest "$name")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    return "$rc"
}

load_file_map

print_header "Media Mover"

units_changed=false
timer_changed=false

rc=0
install_build_file "homelab-media-mover.service" || rc=$?
[[ $rc -eq 0 ]] && units_changed=true

rc=0
install_build_file "homelab-media-mover-now.service" || rc=$?
[[ $rc -eq 0 ]] && units_changed=true

rc=0
install_build_file "homelab-media-mover.timer" || rc=$?
[[ $rc -eq 0 ]] && timer_changed=true && units_changed=true

rc=0
install_build_file "homelab-media-mover.py" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]]

rc=0
install_build_file "media-mover.env" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]]

if [[ -f "$BUILD_DIR/media-mover.local.env" ]]; then
    rc=0
    install_build_file "media-mover.local.env" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]]
fi

if [[ -f "/usr/local/bin/homelab-media-mover-now" ]]; then
    rm -f "/usr/local/bin/homelab-media-mover-now"
    print_sub "Removed /usr/local/bin/homelab-media-mover-now"
fi

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

ensure_timer_state homelab-media-mover.timer "$ENABLE_MEDIA_MOVER_TIMER" "$timer_changed"

print_header "Media Mover Complete"
