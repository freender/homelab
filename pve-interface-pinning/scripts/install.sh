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

# Look for other on-host mechanisms that could also rename/match the interfaces we are
# about to pin, and warn about them. Best-effort and non-fatal: a false positive here
# must never block a deploy, so this only ever prints print_warn.
#
# systemd-networkd merges *.link files from /etc, /run, and /usr/lib (vendor defaults)
# by filename across all three, so a foreign file elsewhere in that search path can
# still win the rename race even though nothing here touches it directly.
check_competing_link_files() {
    local build_dir="$1"
    local link_files_conf="$2"
    local -a managed=()
    local link_file dir existing base mac name managed_here

    if [[ -f "$link_files_conf" ]]; then
        while IFS= read -r link_file; do
            [[ -n "$link_file" ]] && managed+=("$link_file")
        done < "$link_files_conf"
    fi

    if [[ -f /etc/udev/rules.d/70-persistent-net.rules ]]; then
        print_warn "legacy /etc/udev/rules.d/70-persistent-net.rules present; may race with pinned .link files"
    fi

    shopt -s nullglob
    for dir in /etc/systemd/network /run/systemd/network /usr/lib/systemd/network /lib/systemd/network; do
        for existing in "$dir"/*.link; do
            base="$(basename "$existing")"
            managed_here=false
            if [[ "$dir" == "/etc/systemd/network" ]]; then
                for link_file in "${managed[@]}"; do
                    [[ "$base" == "$link_file" ]] && managed_here=true && break
                done
            fi
            [[ "$managed_here" == true ]] && continue

            for link_file in "${managed[@]}"; do
                mac="$(grep -im1 '^MACAddress=' "$build_dir/$link_file" 2>/dev/null | cut -d= -f2)"
                name="$(grep -im1 '^Name=' "$build_dir/$link_file" 2>/dev/null | cut -d= -f2)"
                [[ -n "$mac" ]] || continue
                if grep -qi "$mac" "$existing" 2>/dev/null; then
                    print_warn "foreign link file $existing also matches pinned MAC $mac (target name $name); may race with homelab pin"
                elif [[ -n "$name" ]] && grep -q "Name=$name\$" "$existing" 2>/dev/null; then
                    print_warn "foreign link file $existing also targets name $name; may collide with homelab pin"
                fi
            done
        done
    done
    shopt -u nullglob
}

print_action "Competing interface-naming rules"
check_competing_link_files "$BUILD_DIR" "$LINK_FILES"

print_action "PVE interface pinning"
mkdir -p /etc/systemd/network /etc/homelab /usr/local/sbin

# Snapshot the pinned .link destinations before install_file_map overwrites them, so
# the reboot warning below only fires when a pinned name actually changes rather than
# unconditionally on every deploy (including no-op runs where nothing changed).
link_changed=false
if [[ -f "$LINK_FILES" ]]; then
    while IFS= read -r link_file; do
        [[ -n "$link_file" ]] || continue
        dest="/etc/systemd/network/$link_file"
        before=""
        [[ -f "$dest" ]] && before="$(cat "$dest")"
        after=""
        [[ -f "$BUILD_DIR/$link_file" ]] && after="$(cat "$BUILD_DIR/$link_file")"
        [[ "$before" != "$after" ]] && link_changed=true
    done < "$LINK_FILES"
fi

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

if [[ "$link_changed" == true ]]; then
    print_warn "Pinned interface name(s) changed; systemd-networkd applies renames on next boot only -- review /etc/network/interfaces before rebooting"
else
    print_sub "Pinned interface names unchanged; no reboot needed for interface naming"
fi
