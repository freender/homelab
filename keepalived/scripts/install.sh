#!/bin/bash

set -e

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -n env FORCE_UPDATE="${FORCE_UPDATE:-false}" bash "$0" "$@"
fi

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

print_header "Keepalived"

print_action "Package"
if ! command -v keepalived >/dev/null 2>&1; then
    apt-get install -y -q keepalived curl
    print_ok "keepalived installed"
else
    print_sub "keepalived already installed"
fi

install -d -m 755 /etc/keepalived

units_changed=false
rc=0
install_build_file "healthcheck.sh" || rc=$?
[[ $rc -eq 0 ]] && units_changed=true

rc=0
install_build_file "keepalived.conf" || rc=$?
[[ $rc -eq 0 ]] && units_changed=true

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

if ! systemctl is-enabled --quiet keepalived 2>/dev/null; then
    systemctl enable --now keepalived
    print_ok "keepalived enabled"
elif [[ "$units_changed" == "true" ]]; then
    systemctl restart keepalived
    print_ok "keepalived restarted"
elif ! systemctl is-active --quiet keepalived 2>/dev/null; then
    systemctl start keepalived
    print_ok "keepalived started"
else
    print_sub "keepalived already enabled"
fi

print_header "Keepalived Complete"
