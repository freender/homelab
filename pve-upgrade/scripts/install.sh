#!/usr/bin/env bash
# install.sh - On-demand apt dist-upgrade for PVE/PBS/PDM (Debian-based) hosts
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
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

print_header "PVE/PBS/PDM Upgrade"

if [[ "$PAUSED" == "true" ]]; then
    print_sub "Paused via pve-upgrade.paused; skipping apt dist-upgrade on $(hostname)"
    exit 0
fi

print_action "Running apt-get update"
apt-get update

print_action "Running apt-get dist-upgrade"
DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade

print_ok "Upgrade complete on $(hostname)"

# Warn only: never reboot automatically, so a live PVE cluster node or PBS/PDM
# host is never rebooted unattended by a deploy run.
#
# PVE/PBS do not ship update-notifier-common, so /var/run/reboot-required is
# never populated there even after a kernel package upgrade. Fall back to
# comparing the running kernel against the newest kernel image in /boot
# (LXC hosts with no /boot/vmlinuz-* have no kernel of their own and are
# skipped automatically).
reboot_required=false
reboot_reason=""

if [[ -f /var/run/reboot-required ]]; then
    reboot_required=true
    reboot_reason="reboot-required flag"
fi

running_kernel=$(uname -r)
newest_kernel=""
if compgen -G "/boot/vmlinuz-*" >/dev/null 2>&1; then
    newest_kernel=$(
        for f in /boot/vmlinuz-*; do basename "$f" | sed 's/^vmlinuz-//'; done \
            | sort -V | tail -1
    )
fi

if [[ -n "$newest_kernel" && "$newest_kernel" != "$running_kernel" ]]; then
    reboot_required=true
    reboot_reason="running $running_kernel, newest installed $newest_kernel"
fi

if [[ "$reboot_required" == true ]]; then
    print_warn "Reboot required on $(hostname) ($reboot_reason)"
fi
