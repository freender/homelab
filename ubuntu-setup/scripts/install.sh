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

# load_file_map/mapped_dest/mapped_mode/install_build_file come from lib/utils.sh
# (sourced above) and already carry the "missing file-map entry" error guard;
# do not redefine them here.
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

# timedatectl set-timezone updates /etc/localtime itself on systemd hosts, but
# match pve-postinstall's belt-and-suspenders approach (also used when
# timedatectl is unavailable or silently no-ops) so /etc/localtime and
# /etc/timezone are always consistent with SYSTEM_TIMEZONE.
if [[ -e "/usr/share/zoneinfo/$SYSTEM_TIMEZONE" ]]; then
    ln -snf "/usr/share/zoneinfo/$SYSTEM_TIMEZONE" /etc/localtime || print_warn "failed to update /etc/localtime"
    printf '%s\n' "$SYSTEM_TIMEZONE" > /etc/timezone || print_warn "failed to write /etc/timezone"
else
    print_warn "timezone data not found for $SYSTEM_TIMEZONE"
fi

print_action "Unwanted default services"
# openipmi: LSB init script that fails at boot on hardware with no BMC/IPMI
# device. Masks cleanly as a no-op if the package is not installed (e.g. cinci).
homelab_mask_unwanted_service openipmi.service "no IPMI hardware on this host"

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
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

print_action "SSH hardening"
sshd_changed=false
# 01-disable-password-auth.conf was ubuntu-setup's naming before it was unified
# with pve-postinstall's 99-disable-password-auth.conf convention; clean it up
# wherever a prior deploy left it behind.
legacy_sshd_hardening="/etc/ssh/sshd_config.d/01-disable-password-auth.conf"
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
if [[ $rc -gt 1 ]]; then
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
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    update-initramfs -u
    echo "$ZFS_ARC_MAX" > /sys/module/zfs/parameters/zfs_arc_max
    print_ok "ZFS ARC limit applied"
fi

print_action "Inotify limits"
rc=0
install_build_file "99-inotify.conf" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
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
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
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

print_header "Ubuntu Setup Complete"
