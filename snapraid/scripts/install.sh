#!/usr/bin/env bash

set -euo pipefail

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

print_header "Installing SnapRAID"

# 1. Install snapraid package
if ! command -v snapraid >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q snapraid
    print_ok "snapraid installed"
else
    print_sub "snapraid already installed"
fi

units_changed=false
timer_changed=false

for unit in snapraid.conf snapraid-sync.service snapraid-sync.timer; do
    rc=0
    install_build_file "$unit" || rc=$?
    if [[ $rc -eq 0 ]]; then
        if [[ "$unit" == "snapraid-sync.timer" ]]; then
            timer_changed=true
        fi
        units_changed=true
    fi
done

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

if ! systemctl is-enabled --quiet snapraid-sync.timer 2>/dev/null; then
    systemctl enable --now snapraid-sync.timer
    print_ok "snapraid-sync.timer enabled"
elif [[ "$timer_changed" == "true" ]]; then
    systemctl restart snapraid-sync.timer
    print_ok "snapraid-sync.timer restarted"
elif ! systemctl is-active --quiet snapraid-sync.timer 2>/dev/null; then
    systemctl start snapraid-sync.timer
    print_ok "snapraid-sync.timer started"
else
    print_sub "snapraid-sync.timer already enabled"
fi
