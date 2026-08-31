#!/bin/bash
# install.sh - Enable Debian-security-only unattended upgrades on a host whose
# full dist-upgrade is deliberately manual (the PVE nodes).
#
# Usage: ./scripts/install.sh [hostname]
set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}
PAUSED=${PAUSED:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

if [[ ${EUID} -ne 0 ]]; then
    print_error "must run as root"
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1
load_file_map "$BUILD_DIR/file-map.conf"

print_header "APT Security Updates"

# Pausing disables apt-daily-upgrade.timer, which exists solely to invoke
# unattended-upgrades -- so the host keeps its config and its package index
# refresh (apt-daily.timer) but stops installing anything on its own.
if homelab_apply_pause "$PAUSED" apt-daily-upgrade.timer; then
    print_header "APT Security Updates Complete (paused)"
    exit 0
fi

# This module and apt-upgrade are mutually exclusive by design. apt-upgrade runs
# a full `apt-get -y dist-upgrade` on a timer, which would upgrade Proxmox
# packages and the kernel unattended -- precisely what this module exists to
# prevent. Deploying both to one host would leave the narrow origin scope in
# place while something else ignored it, so fail rather than silently produce a
# host that is not what either module claims.
assert_no_conflicting_dist_upgrade() {
    if systemctl list-unit-files homelab-apt-dist-upgrade.timer >/dev/null 2>&1 &&
       [[ -e /etc/systemd/system/homelab-apt-dist-upgrade.timer ]]; then
        print_error "homelab-apt-dist-upgrade.timer is installed on this host"
        print_sub "apt-upgrade performs a full dist-upgrade and would defeat the"
        print_sub "security-only scope of apt-security-updates. Remove the"
        print_sub "apt-upgrade feature from this host in hosts.conf, or do not"
        print_sub "enable apt-security-updates here."
        return 1
    fi
    return 0
}

assert_no_conflicting_dist_upgrade || exit 1

if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
    print_action "Installing unattended-upgrades"
    apt-get update -qq
    apt-get install -y -qq unattended-upgrades
    print_ok "unattended-upgrades installed"
else
    print_sub "unattended-upgrades already installed"
fi

rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

# Verify the *effective* configuration rather than the file we just wrote. The
# #clear directives in 52homelab-security-updates are what stop the packaged
# 50unattended-upgrades defaults from remaining active alongside ours, and an
# APT list append is silent when it goes wrong -- the file would look correct
# while the resolved policy was wider than intended. apt-config dump reports
# what unattended-upgrades will actually read.
verify_origins_scope() {
    local dump
    dump="$(apt-config dump Unattended-Upgrade::Origins-Pattern 2>/dev/null || true)"

    if [[ -z "$dump" ]]; then
        print_error "Unattended-Upgrade::Origins-Pattern resolved to nothing"
        return 1
    fi
    if ! printf '%s' "$dump" | grep -q 'Debian-Security'; then
        print_error "resolved Origins-Pattern does not include Debian-Security"
        printf '%s\n' "$dump" >&2
        return 1
    fi
    # Fails closed: if a #clear ever stops working, the packaged defaults or a
    # hand-edit could reintroduce a wider origin, and this is the check that
    # refuses to leave the host in that state.
    if printf '%s' "$dump" | grep -qiE 'proxmox|stable-updates|backports'; then
        print_error "resolved Origins-Pattern reaches beyond Debian security"
        printf '%s\n' "$dump" >&2
        return 1
    fi

    print_ok "Origins-Pattern scoped to Debian security only"
    return 0
}

verify_origins_scope || exit 1

systemctl enable --now apt-daily.timer
systemctl enable --now apt-daily-upgrade.timer

systemctl is-active --quiet apt-daily.timer
systemctl is-active --quiet apt-daily-upgrade.timer

print_ok "Security-only unattended upgrades active on $HOST"
