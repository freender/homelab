#!/bin/bash

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

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/env" "$BUILD_DIR/env" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/env"

APPDATA_SCRIPTS_DIR="${ZFS_MOUNTPOINT}/appdata/scripts"
REBUILD_BUNDLE_ROOT="${REBUILD_BUNDLE_ROOT:-${APPDATA_SCRIPTS_DIR}/zfs-automation}"
REBUILD_BUNDLE_BUILD_DIR="${REBUILD_BUNDLE_ROOT}/build/${HOST}"
REBUILD_BUNDLE_SCRIPTS_DIR="${REBUILD_BUNDLE_ROOT}/scripts"
REBUILD_BUNDLE_LIB_DIR="${REBUILD_BUNDLE_ROOT}/lib"

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

mapped_dest() { printf '%s\n' "${FILE_MAP_DEST[$1]}"; }
mapped_mode() { printf '%s\n' "${FILE_MAP_MODE[$1]:-644}"; }

install_build_file() {
    local name="$1"
    local rc=0
    install_if_changed "$BUILD_DIR/$name" "$(mapped_dest "$name")" "$(mapped_mode "$name")" "$(mapped_dest "$name")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    return "$rc"
}

load_file_map

sync_rebuild_bundle() {
    local changed=false
    local file
    local file_name
    local rc

    mkdir -p "$REBUILD_BUNDLE_BUILD_DIR" "$REBUILD_BUNDLE_SCRIPTS_DIR" "$REBUILD_BUNDLE_LIB_DIR"

    shopt -s nullglob
    for file in "$BUILD_DIR"/*; do
        file_name="$(basename "$file")"
        rc=0
        install_if_changed "$file" "$REBUILD_BUNDLE_BUILD_DIR/$file_name" "644" "$REBUILD_BUNDLE_BUILD_DIR/$file_name" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
        [[ $rc -eq 0 ]] && changed=true
    done
    shopt -u nullglob

    rc=0
    install_if_changed "$SCRIPT_DIR/scripts/install.sh" "$REBUILD_BUNDLE_SCRIPTS_DIR/install.sh" "755" "$REBUILD_BUNDLE_SCRIPTS_DIR/install.sh" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
    [[ $rc -eq 0 ]] && changed=true

    for file_name in utils.sh print.sh; do
        rc=0
        install_if_changed "$SCRIPT_DIR/lib/$file_name" "$REBUILD_BUNDLE_LIB_DIR/$file_name" "644" "$REBUILD_BUNDLE_LIB_DIR/$file_name" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
        [[ $rc -eq 0 ]] && changed=true
    done

    if [[ -n "$DEPLOY_USER" ]] && id "$DEPLOY_USER" >/dev/null 2>&1; then
        chown -R "$DEPLOY_USER:$DEPLOY_USER" "$REBUILD_BUNDLE_ROOT"
    fi

    if [[ "$changed" == true ]]; then
        print_ok "Rebuild bundle synced to $REBUILD_BUNDLE_ROOT"
    else
        print_sub "Rebuild bundle already up to date"
    fi
}

print_header "ZFS Automation"

print_action "Sanoid / Syncoid"
if ! command -v sanoid >/dev/null 2>&1 || ! command -v syncoid >/dev/null 2>&1; then
    apt-get install -y -q sanoid
    print_ok "Sanoid installed"
else
    print_sub "Sanoid already installed"
fi

mkdir -p /etc/sanoid "$APPDATA_SCRIPTS_DIR"

rc=0
install_build_file "sanoid.conf" || rc=$?
if [[ $rc -eq 0 ]]; then
    print_ok "sanoid.conf updated"
fi

for helper in sanoid.conf homelab-zfs-snapshots.service homelab-zfs-snapshots.timer homelab-zfs-replication.service homelab-zfs-replication.timer zfs-scrub.service zfs-scrub.timer homelab-zfs-health-check.service homelab-zfs-health-check.timer; do
    rc=0
    install_if_changed "$BUILD_DIR/$helper" "$APPDATA_SCRIPTS_DIR/$helper" "644" "$APPDATA_SCRIPTS_DIR/$helper" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
done

units_changed=false
for unit in homelab-zfs-snapshots.service homelab-zfs-snapshots.timer homelab-zfs-replication.service homelab-zfs-replication.timer zfs-scrub.service zfs-scrub.timer homelab-zfs-health-check.service homelab-zfs-health-check.timer; do
    rc=0
    install_build_file "$unit" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && units_changed=true
done

if systemctl is-enabled --quiet sanoid.timer 2>/dev/null; then
    systemctl disable --now sanoid.timer
    print_ok "Disabled packaged sanoid.timer"
fi

if [[ "$units_changed" == true ]]; then
    systemctl daemon-reload
fi

for timer in homelab-zfs-snapshots.timer homelab-zfs-replication.timer zfs-scrub.timer homelab-zfs-health-check.timer; do
    if ! systemctl is-enabled --quiet "$timer" 2>/dev/null; then
        systemctl enable --now "$timer"
        print_ok "$timer enabled"
    elif [[ "$units_changed" == true ]]; then
        systemctl restart "$timer"
        print_ok "$timer restarted"
    else
        print_sub "$timer already enabled"
    fi
done

print_action "Rebuild bundle"
sync_rebuild_bundle

print_header "ZFS Automation Complete"
