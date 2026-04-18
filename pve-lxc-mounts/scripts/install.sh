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
require_file "$BUILD_DIR/containers.conf" "$BUILD_DIR/containers.conf" || exit 1

prepare_mount_targets() {
    local ctid="$1"
    local config_path="$2"
    local rootfs="/var/lib/lxc/${ctid}/rootfs"
    local line
    local entry
    local source
    local target
    local fstype
    local options
    local rest
    local target_path

    require_dir "$rootfs" "$rootfs" || exit 1

    while IFS= read -r line; do
        [[ "$line" == lxc.mount.entry:* ]] || continue

        entry="${line#lxc.mount.entry: }"
        _source=""
        target=""
        _fstype=""
        options=""
        rest=""
        IFS=' ' read -r _source target _fstype options rest <<< "$entry"

        [[ -n "$target" ]] || continue
        [[ "$options" == *"create=dir"* ]] || continue

        target_path="${rootfs}/${target#/}"
        if [[ ! -d "$target_path" ]]; then
            mkdir -p "$target_path"
            print_sub "Created CT $ctid mount target /${target#/}"
        fi
    done < "$config_path"
}

restart_container_if_running() {
    local ctid="$1"
    local config_path="$2"
    local status

    status="$(pct status "$ctid")"
    if [[ "$status" == *"status: running"* ]]; then
        print_sub "Restarting CT $ctid to apply mount changes..."
        pct stop "$ctid"
        prepare_mount_targets "$ctid" "$config_path"
        pct start "$ctid"
        return
    fi

    prepare_mount_targets "$ctid" "$config_path"
    print_sub "CT $ctid is stopped; config updated without restart"
}

while IFS= read -r ctid; do
    [[ -n "$ctid" ]] || continue

    src="$BUILD_DIR/${ctid}.conf"
    dst="/etc/pve/lxc/${ctid}.conf"
    require_file "$src" "$src" || exit 1

    rc=0
    backup_and_copy_if_changed "$src" "$dst" "$dst" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

    if [[ $rc -eq 0 ]]; then
        restart_container_if_running "$ctid" "$src"
    fi
done < "$BUILD_DIR/containers.conf"
