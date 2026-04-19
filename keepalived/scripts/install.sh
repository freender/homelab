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
