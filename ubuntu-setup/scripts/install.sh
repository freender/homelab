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
require_file "$SCRIPT_DIR/scripts/docker-install.sh" "$SCRIPT_DIR/scripts/docker-install.sh" || exit 1
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

mkdir -p "$APPDATA_ROOT" "$APPDATA_SCRIPTS_DIR"

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
install_if_changed \
    "$SCRIPT_DIR/scripts/pin-primary-nic.sh" \
    "$APPDATA_SCRIPTS_DIR/pin-primary-nic.sh" \
    "755" \
    "$APPDATA_SCRIPTS_DIR/pin-primary-nic.sh" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

print_action "Docker CE"
ensure_docker_installed

rc=0
install_if_changed \
    "$SCRIPT_DIR/scripts/docker-install.sh" \
    "$APPDATA_SCRIPTS_DIR/docker-install.sh" \
    "755" \
    "$APPDATA_SCRIPTS_DIR/docker-install.sh" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

if [[ -n "$DEPLOY_USER" ]]; then
    if ! id -nG "$DEPLOY_USER" | grep -qw docker; then
        usermod -aG docker "$DEPLOY_USER"
        print_ok "Added $DEPLOY_USER to docker group"
    else
        print_sub "$DEPLOY_USER already in docker group"
    fi
fi

if [[ "$ZFS_AUTOMATION_ENABLED" == "true" ]]; then
    print_action "Sanoid / Syncoid"

    if ! command -v sanoid >/dev/null 2>&1 || ! command -v syncoid >/dev/null 2>&1; then
        apt-get install -y -q sanoid
        print_ok "Sanoid installed"
    else
        print_sub "Sanoid already installed"
    fi

    mkdir -p /etc/sanoid

    rc=0
    install_if_changed "$BUILD_DIR/sanoid.conf" "/etc/sanoid/sanoid.conf" "644" "/etc/sanoid/sanoid.conf" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    if [[ $rc -eq 0 ]]; then
        print_ok "sanoid.conf updated"
    fi

    for helper in \
        sanoid.conf \
        homelab-zfs-snapshots.service \
        homelab-zfs-snapshots.timer \
        homelab-zfs-replication.service \
        homelab-zfs-replication.timer
    do
        mode="644"
        case "$helper" in
            *.sh) mode="755" ;;
        esac
        rc=0
        install_if_changed \
            "$BUILD_DIR/$helper" \
            "$APPDATA_SCRIPTS_DIR/$helper" \
            "$mode" \
            "$APPDATA_SCRIPTS_DIR/$helper" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    done

    zfs_units_changed=false
    for unit in \
        homelab-zfs-snapshots.service \
        homelab-zfs-snapshots.timer \
        homelab-zfs-replication.service \
        homelab-zfs-replication.timer
    do
        rc=0
        install_if_changed \
            "$BUILD_DIR/$unit" \
            "/etc/systemd/system/$unit" \
            "644" \
            "$unit" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
        [[ $rc -eq 0 ]] && zfs_units_changed=true
    done

    if systemctl is-enabled --quiet sanoid.timer 2>/dev/null; then
        systemctl disable --now sanoid.timer
        print_ok "Disabled packaged sanoid.timer"
    fi

    if [[ "$zfs_units_changed" == true ]]; then
        systemctl daemon-reload
    fi

    for timer in homelab-zfs-snapshots.timer homelab-zfs-replication.timer; do
        if ! systemctl is-enabled --quiet "$timer" 2>/dev/null; then
            systemctl enable --now "$timer"
            print_ok "$timer enabled"
        elif [[ "$zfs_units_changed" == true ]]; then
            systemctl restart "$timer"
            print_ok "$timer restarted"
        else
            print_sub "$timer already enabled"
        fi
    done
fi

print_action "Sudoers"
SUDOERS_FILE="/etc/sudoers.d/99-${DEPLOY_USER}-homelab"
rc=0
install_if_changed "$BUILD_DIR/sudoers" "$SUDOERS_FILE" "440" "$SUDOERS_FILE" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    visudo -cf "$SUDOERS_FILE"
fi

print_action "SSH hardening"
SSH_CONF="/etc/ssh/sshd_config.d/99-disable-password-auth.conf"
rc=0
install_if_changed "$BUILD_DIR/sshd-hardening.conf" "$SSH_CONF" "644" "$SSH_CONF" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    sshd -t
    systemctl reload ssh
    print_ok "SSH reloaded"
fi

print_action "ZFS ARC limit"
rc=0
install_if_changed "$BUILD_DIR/zfs.conf" "/etc/modprobe.d/zfs.conf" "644" "/etc/modprobe.d/zfs.conf" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    update-initramfs -u
    echo "$ZFS_ARC_MAX" > /sys/module/zfs/parameters/zfs_arc_max
    print_ok "ZFS ARC limit applied"
fi

print_action "Inotify limits"
rc=0
install_if_changed \
    "$BUILD_DIR/99-inotify.conf" \
    "/etc/sysctl.d/99-inotify.conf" \
    "644" \
    "/etc/sysctl.d/99-inotify.conf" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    sysctl --system >/dev/null
    print_ok "Inotify limits applied"
fi

if [[ "$WIREGUARD_ENABLED" == "true" ]]; then
    print_action "WireGuard sysctl"
    rc=0
    install_if_changed \
        "$BUILD_DIR/99-wireguard.conf" \
        "/etc/sysctl.d/99-wireguard.conf" \
        "644" \
        "/etc/sysctl.d/99-wireguard.conf" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    if [[ $rc -eq 0 ]]; then
        sysctl --system >/dev/null
        print_ok "WireGuard sysctl applied"
    fi
fi

print_action "TRIM"
if ! systemctl is-enabled --quiet fstrim.timer 2>/dev/null; then
    systemctl enable --now fstrim.timer
    print_ok "fstrim.timer enabled"
else
    print_sub "fstrim.timer already enabled"
fi

if [[ "$(zpool get -H -o value autotrim "$ZFS_POOL" 2>/dev/null)" != "on" ]]; then
    zpool set autotrim=on "$ZFS_POOL"
    print_ok "autotrim enabled on $ZFS_POOL"
else
    print_sub "autotrim already on for $ZFS_POOL"
fi

print_action "ZFS scrub timer"
changed=false
rc=0
install_if_changed \
    "$BUILD_DIR/zfs-scrub.service" \
    "/etc/systemd/system/zfs-scrub.service" \
    "644" \
    "zfs-scrub.service" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
[[ $rc -eq 0 ]] && changed=true

rc=0
install_if_changed \
    "$BUILD_DIR/zfs-scrub.timer" \
    "/etc/systemd/system/zfs-scrub.timer" \
    "644" \
    "zfs-scrub.timer" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
[[ $rc -eq 0 ]] && changed=true

if [[ "$changed" == true ]]; then
    systemctl daemon-reload
fi

if ! systemctl is-enabled --quiet zfs-scrub.timer 2>/dev/null; then
    systemctl enable --now zfs-scrub.timer
    print_ok "zfs-scrub.timer enabled"
else
    if [[ "$changed" == true ]]; then
        systemctl restart zfs-scrub.timer
        print_ok "zfs-scrub.timer restarted"
    else
        print_sub "zfs-scrub.timer already enabled"
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
        "/etc/samba/smb.conf" \
        "644" \
        "/etc/samba/smb.conf" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    if [[ $rc -eq 0 ]]; then
        systemctl restart smbd
        print_ok "Samba restarted"
    fi
fi

print_action "Failure notifications"
rc=0
install_if_changed \
    "$SCRIPT_DIR/scripts/notify-failure.sh" \
    "/usr/local/bin/homelab-notify-failure" \
    "755" \
    "/usr/local/bin/homelab-notify-failure" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    print_ok "notify-failure.sh deployed"
fi

notify_unit_changed=false
rc=0
install_if_changed \
    "$BUILD_DIR/homelab-notify-failure@.service" \
    "/etc/systemd/system/homelab-notify-failure@.service" \
    "644" \
    "homelab-notify-failure@.service" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
[[ $rc -eq 0 ]] && notify_unit_changed=true

if [[ "$notify_unit_changed" == true ]]; then
    systemctl daemon-reload
    print_ok "homelab-notify-failure@.service deployed"
fi

if [[ "$NOTIFICATIONS_ENABLED" == "true" ]]; then
    mkdir -p /etc/homelab
    rc=0
    install_if_changed \
        "$BUILD_DIR/telegram.env" \
        "/etc/homelab/telegram.env" \
        "600" \
        "/etc/homelab/telegram.env" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    if [[ $rc -eq 0 ]]; then
        print_ok "telegram.env deployed"
    fi
    print_sub "Notifications enabled"
else
    print_sub "No telegram.env in secrets; notifications disabled"
fi

print_action "Rebuild helpers"
rc=0
install_if_changed \
    "$BUILD_DIR/rebuild.sh" \
    "$APPDATA_SCRIPTS_DIR/rebuild.sh" \
    "755" \
    "$APPDATA_SCRIPTS_DIR/rebuild.sh" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    print_ok "rebuild.sh deployed"
fi

print_header "Ubuntu Setup Complete"
