#!/bin/bash
set -euo pipefail

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
LINK_FILES="$BUILD_DIR/link-files.conf"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
load_file_map "$BUILD_DIR/file-map.conf"

print_action "PVE interface pinning"
mkdir -p /etc/systemd/network /etc/homelab /usr/local/sbin

rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

if [[ -f "$LINK_FILES" ]]; then
    while IFS= read -r link_file; do
        [[ -n "$link_file" ]] || continue
        if [[ ! -f "/etc/systemd/network/$link_file" ]]; then
            print_warn "Expected link file was not installed: /etc/systemd/network/$link_file"
        fi
    done < "$LINK_FILES"
fi

udevadm control --reload || print_warn "failed to reload udev rules"
systemctl daemon-reload

if [[ -s /etc/homelab/interface-wol.conf ]]; then
    systemctl enable --now homelab-interface-wol.service >/dev/null
    print_ok "homelab-interface-wol.service enabled"
else
    systemctl disable --now homelab-interface-wol.service >/dev/null 2>&1 || true
    print_sub "No WOL interfaces configured"
fi

print_warn "Interface name changes apply on next boot; review /etc/network/interfaces before rebooting"
