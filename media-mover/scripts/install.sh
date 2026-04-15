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
install_build_file "homelab-media-mover.timer" || rc=$?
[[ $rc -eq 0 ]] && timer_changed=true && units_changed=true

rc=0
install_build_file "homelab-media-mover.py" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]]

rc=0
install_build_file "homelab-media-mover-now" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]]

rc=0
install_build_file "media-mover.env" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]]

if [[ -f "$BUILD_DIR/media-mover.local.env" ]]; then
    rc=0
    install_build_file "media-mover.local.env" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]]
fi

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

if ! systemctl is-enabled --quiet homelab-media-mover.timer 2>/dev/null; then
    systemctl enable --now homelab-media-mover.timer
    print_ok "homelab-media-mover.timer enabled"
elif [[ "$timer_changed" == "true" ]]; then
    systemctl restart homelab-media-mover.timer
    print_ok "homelab-media-mover.timer restarted"
elif ! systemctl is-active --quiet homelab-media-mover.timer 2>/dev/null; then
    systemctl start homelab-media-mover.timer
    print_ok "homelab-media-mover.timer started"
else
    print_sub "homelab-media-mover.timer already enabled"
fi

print_header "Media Mover Complete"
