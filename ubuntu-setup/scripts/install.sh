#!/bin/bash
# install.sh - Ubuntu OS setup: Docker, sudoers, SSH hardening, ZFS tuning, and rebuild helpers.

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
require_file "$SCRIPT_DIR/lib/utils.sh" "$SCRIPT_DIR/lib/utils.sh" || exit 1
require_file "$SCRIPT_DIR/lib/print.sh" "$SCRIPT_DIR/lib/print.sh" || exit 1
require_file "$SCRIPT_DIR/scripts/docker-install.sh" "$SCRIPT_DIR/scripts/docker-install.sh" || exit 1
require_file "$SCRIPT_DIR/scripts/fix_backup_permissions.sh" "$SCRIPT_DIR/scripts/fix_backup_permissions.sh" || exit 1
require_file "$SCRIPT_DIR/scripts/notify-failure.sh" "$SCRIPT_DIR/scripts/notify-failure.sh" || exit 1
require_file "$SCRIPT_DIR/scripts/pin-primary-nic.sh" "$SCRIPT_DIR/scripts/pin-primary-nic.sh" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/env"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/scripts/docker-install.sh"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/scripts/pin-primary-nic.sh"

APPDATA_ROOT="${ZFS_MOUNTPOINT}/appdata"
APPDATA_SCRIPTS_DIR="${APPDATA_ROOT}/scripts"
REBUILD_BUNDLE_ROOT="${REBUILD_BUNDLE_ROOT:-${APPDATA_SCRIPTS_DIR}/ubuntu-setup}"
REBUILD_BUNDLE_BUILD_DIR="${REBUILD_BUNDLE_ROOT}/build/${HOST}"
REBUILD_BUNDLE_SCRIPTS_DIR="${REBUILD_BUNDLE_ROOT}/scripts"
REBUILD_BUNDLE_LIB_DIR="${REBUILD_BUNDLE_ROOT}/lib"

mkdir -p "$APPDATA_ROOT" "$APPDATA_SCRIPTS_DIR"

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
    local name="$1"
    printf '%s\n' "${FILE_MAP_DEST[$name]}"
}

mapped_mode() {
    local name="$1"
    printf '%s\n' "${FILE_MAP_MODE[$name]:-644}"
}

install_build_file() {
    local name="$1"
    local rc=0

    install_if_changed "$BUILD_DIR/$name" "$(mapped_dest "$name")" "$(mapped_mode "$name")" "$(mapped_dest "$name")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    return "$rc"
}

install_script_file() {
    local name="$1"
    local rc=0

    install_if_changed "$SCRIPT_DIR/scripts/$name" "$(mapped_dest "$name")" "$(mapped_mode "$name")" "$(mapped_dest "$name")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    return "$rc"
}

load_file_map

sync_rebuild_bundle() {
    local changed=false
    local file
    local file_name
    local mode
    local rc

    mkdir -p "$REBUILD_BUNDLE_BUILD_DIR" "$REBUILD_BUNDLE_SCRIPTS_DIR" "$REBUILD_BUNDLE_LIB_DIR"

    shopt -s nullglob
    for file in "$BUILD_DIR"/*; do
        file_name="$(basename "$file")"
        mode="644"
        if [[ "$file_name" == "rebuild.sh" ]]; then
            mode="755"
        fi
        rc=0
        install_if_changed "$file" "$REBUILD_BUNDLE_BUILD_DIR/$file_name" "$mode" "$REBUILD_BUNDLE_BUILD_DIR/$file_name" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
        [[ $rc -eq 0 ]] && changed=true
    done
    shopt -u nullglob

    for file_name in install.sh docker-install.sh fix_backup_permissions.sh notify-failure.sh pin-primary-nic.sh; do
        rc=0
        install_if_changed \
            "$SCRIPT_DIR/scripts/$file_name" \
            "$REBUILD_BUNDLE_SCRIPTS_DIR/$file_name" \
            "755" \
            "$REBUILD_BUNDLE_SCRIPTS_DIR/$file_name" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
        [[ $rc -eq 0 ]] && changed=true
    done

    for file_name in utils.sh print.sh; do
        rc=0
        install_if_changed \
            "$SCRIPT_DIR/lib/$file_name" \
            "$REBUILD_BUNDLE_LIB_DIR/$file_name" \
            "644" \
            "$REBUILD_BUNDLE_LIB_DIR/$file_name" || rc=$?
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

print_header "Ubuntu Setup"

print_action "Hostname and timezone"
if [[ "$(hostnamectl status --static)" != "$SYSTEM_HOSTNAME" ]]; then
    hostnamectl set-hostname "$SYSTEM_HOSTNAME"
    print_ok "Hostname set to $SYSTEM_HOSTNAME"
else
    print_sub "Hostname already set to $SYSTEM_HOSTNAME"
fi

if [[ "$(timedatectl show --property=Timezone --value)" != "$SYSTEM_TIMEZONE" ]]; then
    timedatectl set-timezone "$SYSTEM_TIMEZONE"
    print_ok "Timezone set to $SYSTEM_TIMEZONE"
else
    print_sub "Timezone already set to $SYSTEM_TIMEZONE"
fi

print_action "Primary NIC pinning"
mkdir -p /etc/udev/rules.d
nic_rule_before=""
if [[ -f /etc/udev/rules.d/10-network-names.rules ]]; then
    nic_rule_before="$(cat /etc/udev/rules.d/10-network-names.rules)"
fi
if write_primary_nic_rule \
    /etc/udev/rules.d/10-network-names.rules \
    "$PRIMARY_INTERFACE_NAME" \
    "$PRIMARY_INTERFACE_MAC"
then
    print_ok "Pinned primary interface as $PRIMARY_INTERFACE_NAME"
    nic_rule_after="$(cat /etc/udev/rules.d/10-network-names.rules)"
    if [[ "$nic_rule_before" != "$nic_rule_after" ]]; then
        print_warn "Primary NIC pinning changed; reboot may be required before interface-name-dependent services are reliable"
    fi
else
    print_warn "Could not determine a primary ethernet interface to pin"
fi

rc=0
install_script_file "pin-primary-nic.sh" || rc=$?

print_action "Docker CE"
ensure_docker_installed

rc=0
install_script_file "docker-install.sh" || rc=$?

rc=0
install_script_file "fix_backup_permissions.sh" || rc=$?

if [[ -n "$DEPLOY_USER" ]]; then
    if ! id -nG "$DEPLOY_USER" | grep -qw docker; then
        usermod -aG docker "$DEPLOY_USER"
        print_ok "Added $DEPLOY_USER to docker group"
    else
        print_sub "$DEPLOY_USER already in docker group"
    fi
fi

print_action "Sudoers"
rc=0
install_build_file "sudoers" || rc=$?
if [[ $rc -eq 0 ]]; then
    visudo -cf "$(mapped_dest "sudoers")"
fi

print_action "SSH hardening"
rc=0
install_build_file "sshd-hardening.conf" || rc=$?
if [[ $rc -eq 0 ]]; then
    sshd -t
    systemctl reload ssh
    print_ok "SSH reloaded"
fi

print_action "ZFS ARC limit"
rc=0
install_build_file "zfs.conf" || rc=$?
if [[ $rc -eq 0 ]]; then
    update-initramfs -u
    echo "$ZFS_ARC_MAX" > /sys/module/zfs/parameters/zfs_arc_max
    print_ok "ZFS ARC limit applied"
fi

print_action "Inotify limits"
rc=0
install_build_file "99-inotify.conf" || rc=$?
if [[ $rc -eq 0 ]]; then
    sysctl --system >/dev/null
    print_ok "Inotify limits applied"
fi

if [[ "$WIREGUARD_ENABLED" == "true" ]]; then
    print_action "WireGuard sysctl"
    rc=0
    install_build_file "99-wireguard.conf" || rc=$?
    if [[ $rc -eq 0 ]]; then
        sysctl --system >/dev/null
        print_ok "WireGuard sysctl applied"
    fi
fi

if [[ "$SAMBA_ENABLED" == "true" ]]; then
    print_action "Samba"
    require_file "$BUILD_DIR/smb.conf" "$BUILD_DIR/smb.conf" || exit 1

    if ! command -v smbd >/dev/null 2>&1; then
        print_sub "Installing Samba..."
        apt-get install -y -q samba
        print_ok "Samba installed"
    fi

    rc=0
    backup_and_install_if_changed \
        "$BUILD_DIR/smb.conf" \
        "$(mapped_dest "smb.conf")" \
        "$(mapped_mode "smb.conf")" \
        "$(mapped_dest "smb.conf")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    if [[ $rc -eq 0 ]]; then
        systemctl restart smbd
        print_ok "Samba restarted"
    fi
fi

print_action "Failure notifications"
rc=0
install_script_file "notify-failure.sh" || rc=$?
if [[ $rc -eq 0 ]]; then
    print_ok "notify-failure.sh deployed"
fi

notify_unit_changed=false
rc=0
install_build_file "homelab-notify-failure@.service" || rc=$?
[[ $rc -eq 0 ]] && notify_unit_changed=true

if [[ "$notify_unit_changed" == true ]]; then
    systemctl daemon-reload
    print_ok "homelab-notify-failure@.service deployed"
fi

if [[ "$NOTIFICATIONS_ENABLED" == "true" ]]; then
    mkdir -p /etc/homelab
    rc=0
    install_build_file "telegram.env" || rc=$?
    if [[ $rc -eq 0 ]]; then
        print_ok "telegram.env deployed"
    fi
    print_sub "Notifications enabled"
else
    print_sub "No telegram.env in secrets; notifications disabled"
fi

print_action "Rebuild helpers"
rc=0
install_build_file "rebuild.sh" || rc=$?
if [[ $rc -eq 0 ]]; then
    print_ok "rebuild.sh deployed"
fi

print_action "Rebuild bundle"
sync_rebuild_bundle

print_header "Ubuntu Setup Complete"
