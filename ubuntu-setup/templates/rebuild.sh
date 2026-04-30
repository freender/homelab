#!/bin/bash
# rebuild.sh - Restore ubuntu-setup from the persisted homelab rebuild bundle
# Assumes: ZFS data disk already imported (zpool import -f {{ ZFS_POOL }})
# Usage: sudo bash {{ HOMELAB_STATE_DIR }}/ubuntu-setup/rebuild.sh

set -e

ZFS_POOL="{{ ZFS_POOL }}"
STORAGE_MOUNTPOINT="{{ STORAGE_MOUNTPOINT }}"
HOMELAB_STATE_DIR="{{ HOMELAB_STATE_DIR }}"
SYSTEM_HOSTNAME="{{ SYSTEM_HOSTNAME }}"
BUNDLE_ROOT="${HOMELAB_STATE_DIR}/ubuntu-setup"
BUNDLE_INSTALL_SH="${BUNDLE_ROOT}/scripts/install.sh"
BUNDLE_BUILD_DIR="${BUNDLE_ROOT}/build/${SYSTEM_HOSTNAME}"
BUNDLE_UTILS_SH="${BUNDLE_ROOT}/lib/utils.sh"
ZFS_AUTOMATION_BUNDLE_ROOT="${HOMELAB_STATE_DIR}/zfs-automation"
ZFS_AUTOMATION_INSTALL_SH="${ZFS_AUTOMATION_BUNDLE_ROOT}/scripts/install.sh"
ZFS_AUTOMATION_BUILD_DIR="${ZFS_AUTOMATION_BUNDLE_ROOT}/build/${SYSTEM_HOSTNAME}"

info() { echo "==> $*"; }
ok() { echo "    ✓ $*"; }
warn() { echo "    ! $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

info "Preflight checks"

if [[ "$(id -u)" -ne 0 ]]; then
    die "Run as root: sudo bash $0"
fi

if ! mountpoint -q "$STORAGE_MOUNTPOINT"; then
    die "$STORAGE_MOUNTPOINT is not mounted. Run 'zpool import -f ${ZFS_POOL}' first."
fi

if [[ ! -d "$HOMELAB_STATE_DIR" ]]; then
    die "$HOMELAB_STATE_DIR not found. Re-deploy ubuntu-setup once the box is back online."
fi

if [[ ! -f "$BUNDLE_INSTALL_SH" ]]; then
    die "$BUNDLE_INSTALL_SH not found. Re-deploy ubuntu-setup once the box is back online."
fi

if [[ ! -d "$BUNDLE_BUILD_DIR" ]]; then
    die "$BUNDLE_BUILD_DIR not found. Re-deploy ubuntu-setup once the box is back online."
fi

if [[ ! -f "$BUNDLE_UTILS_SH" ]]; then
    die "$BUNDLE_UTILS_SH not found. Re-deploy ubuntu-setup once the box is back online."
fi

ok "ZFS data disk mounted"
ok "Found persisted ubuntu-setup bundle at $BUNDLE_ROOT"

info "Running bundled ubuntu-setup installer"
bash "$BUNDLE_INSTALL_SH" "$SYSTEM_HOSTNAME"

if [[ -f "$ZFS_AUTOMATION_INSTALL_SH" ]] && [[ -d "$ZFS_AUTOMATION_BUILD_DIR" ]]; then
    info "Running bundled zfs-automation installer"
    bash "$ZFS_AUTOMATION_INSTALL_SH" "$SYSTEM_HOSTNAME"
else
    warn "No persisted zfs-automation bundle found; skipping"
fi

echo ""
echo "=== Rebuild complete ==="
echo ""
echo "Active timers:"
systemctl list-timers --no-pager | grep -E 'homelab|zfs-scrub' || true
