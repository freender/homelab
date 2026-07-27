#!/bin/bash
# install.sh - Ubuntu OS setup: Docker, sudoers, SSH hardening, and ZFS tuning.

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
require_file "$SCRIPT_DIR/scripts/pin-primary-nic.sh" "$SCRIPT_DIR/scripts/pin-primary-nic.sh" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/env"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/scripts/docker-install.sh"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/scripts/pin-primary-nic.sh"

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

load_file_map

cleanup_legacy_rebuild_bundle() {
    local legacy_root="${HOMELAB_STATE_DIR:-/var/lib/homelab}/ubuntu-setup"

    if [[ -d "$legacy_root" ]]; then
        rm -rf "$legacy_root"
        print_ok "Removed legacy local rebuild bundle at $legacy_root"
    fi
}

print_header "Ubuntu Setup"

print_action "Legacy local rebuild bundle"
cleanup_legacy_rebuild_bundle

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

print_action "Unwanted default services"
# openipmi: LSB init script that fails at boot on hardware with no BMC/IPMI
# device. Masks cleanly as a no-op if the package is not installed (e.g. cinci).
if systemctl list-unit-files openipmi.service >/dev/null 2>&1; then
    if [[ "$(systemctl is-enabled openipmi.service 2>/dev/null)" == "masked" ]]; then
        # Idempotent even if already masked: a stale failed record from before
        # it was masked would otherwise trip a failed-unit alert.
        systemctl reset-failed openipmi.service >/dev/null 2>&1 || true
        print_sub "openipmi.service already masked"
    else
        systemctl disable --now openipmi.service >/dev/null 2>&1 || true
        systemctl mask openipmi.service
        systemctl reset-failed openipmi.service >/dev/null 2>&1 || true
        print_ok "openipmi.service masked"
    fi
else
    print_sub "openipmi.service not installed; nothing to mask"
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

print_action "Docker CE"
ensure_docker_installed

if [[ -n "$DEPLOY_USER" ]]; then
    if ! id -nG "$DEPLOY_USER" | grep -qw docker; then
        usermod -aG docker "$DEPLOY_USER"
        print_ok "Added $DEPLOY_USER to docker group"
    else
        print_sub "$DEPLOY_USER already in docker group"
    fi
fi

print_action "Sudoers"
# Validate the staged file BEFORE it lands: an invalid file in /etc/sudoers.d/
# breaks sudo host-wide the instant it is written.
if ! visudo -cf "$BUILD_DIR/sudoers" >/dev/null; then
    print_error "staged sudoers file is invalid; refusing to install"
    exit 1
fi
rc=0
install_build_file "sudoers" || rc=$?

print_action "SSH hardening"
sshd_changed=false
legacy_sshd_hardening="/etc/ssh/sshd_config.d/99-disable-password-auth.conf"
if [[ "$(mapped_dest "sshd-hardening.conf")" != "$legacy_sshd_hardening" && -e "$legacy_sshd_hardening" ]]; then
    rm -f "$legacy_sshd_hardening"
    sshd_changed=true
    print_ok "Removed legacy $(basename "$legacy_sshd_hardening")"
fi
rc=0
# An sshd_config.d drop-in cannot be checked standalone, so install it, validate the
# merged config, and roll back on failure — otherwise a bad drop-in survives on disk
# and locks us out at the next sshd restart.
install_build_file_validated "sshd-hardening.conf" sshd -t || rc=$?
if [[ $rc -eq 2 ]]; then
    print_error "sshd hardening config rejected by sshd -t; rolled back"
    exit 1
fi
[[ $rc -eq 0 ]] && sshd_changed=true
if [[ "$sshd_changed" == true ]]; then
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
    print_action "WireGuard packages"
    if ! command -v wg >/dev/null 2>&1 || ! command -v wg-quick >/dev/null 2>&1; then
        apt-get update -y -q
        apt-get install -y -q wireguard wireguard-tools
        print_ok "WireGuard packages installed"
    else
        print_sub "WireGuard packages already installed"
    fi

    print_action "WireGuard sysctl"
    rc=0
    install_build_file "99-wireguard.conf" || rc=$?
    if [[ $rc -eq 0 ]]; then
        sysctl --system >/dev/null
        print_ok "WireGuard sysctl applied"
    fi

    print_action "WireGuard services"
    shopt -s nullglob
    for conf in /etc/wireguard/*.conf; do
        interface_name="$(basename "$conf" .conf)"
        systemctl enable --now "wg-quick@${interface_name}.service"
        print_ok "wg-quick@${interface_name}.service enabled"
    done
    shopt -u nullglob
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
if [[ "$NOTIFICATIONS_ENABLED" == "true" ]]; then
    # These two are only in file-map.conf (and thus have a resolvable
    # mapped_dest) when the "notifications" feature is enabled -- calling
    # install_build_file for them while disabled fails on an empty
    # destination.
    rc=0
    install_build_file "notify-failure.sh" || rc=$?
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
else
    print_sub "Notifications disabled; not touching notify-failure.sh/service"
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
    # Actively purge, not just skip: ubuntu-setup.notifications: false is used on
    # offsite hosts specifically to keep the Telegram bot token off-host, so a
    # previously-deployed token must not survive turning this off.
    if [[ -e "$TELEGRAM_ENV_DEST" ]]; then
        rm -f "$TELEGRAM_ENV_DEST"
        print_ok "Removed $TELEGRAM_ENV_DEST (notifications disabled)"
    else
        print_sub "No telegram.env in secrets; notifications disabled"
    fi
fi

print_header "Ubuntu Setup Complete"
