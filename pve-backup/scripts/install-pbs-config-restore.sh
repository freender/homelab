#!/bin/bash
# install-pbs-config-restore.sh - Restore PVE config from PBS on fresh standalone installs

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/restore-plan.conf"
ENV_FILE_SOURCE="$BUILD_DIR/pbs.env"
RESTORE_ROOT="/tmp/pve-config-restore"
RESTORE_OUTPUT_DIR="/var/lib/homelab/pve-config-restore"

apply_restored_notifications() {
    local restored_root="$1"
    local source_notifications="$restored_root/notifications.cfg"
    local source_priv_notifications="$restored_root/priv/notifications.cfg"

    if [[ ! -f "$source_notifications" ]]; then
        print_sub "No restored notifications.cfg found; skipping auto-apply"
        return 0
    fi

    if [[ ! -f "$source_priv_notifications" ]]; then
        print_sub "No restored priv/notifications.cfg found; skipping auto-apply"
        return 0
    fi

    mkdir -p /etc/pve/priv
    cp "$source_notifications" /etc/pve/notifications.cfg
    cp "$source_priv_notifications" /etc/pve/priv/notifications.cfg
    chown root:www-data /etc/pve/notifications.cfg /etc/pve/priv/notifications.cfg
    chmod 640 /etc/pve/notifications.cfg
    chmod 600 /etc/pve/priv/notifications.cfg
    print_sub "Auto-applied restored notifications config"
}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
    print_error() { echo "    ✗ Error: $*" >&2; }
fi

if [[ ! -f "$PLAN_FILE" ]]; then
    print_sub "Restore plan not present; skipping PBS config restore"
    exit 0
fi

if [[ ! -f "$ENV_FILE_SOURCE" ]]; then
    print_warn "Missing PBS env file: $ENV_FILE_SOURCE"
    exit 0
fi

if ! command -v proxmox-backup-client >/dev/null 2>&1; then
    print_warn "proxmox-backup-client not found; skipping PBS config restore"
    exit 0
fi

# shellcheck disable=SC1090
source "$PLAN_FILE"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE_SOURCE"
set +a

if [[ -z "${REPOSITORY:-}" || -z "${BACKUP_ID:-}" || -z "${ARCHIVE_NAME:-}" ]]; then
    print_warn "Restore plan missing required values; skipping"
    exit 0
fi

if [[ -z "${PBS_PASSWORD:-}" ]]; then
    print_warn "PBS_PASSWORD missing in $ENV_FILE_SOURCE; skipping"
    exit 0
fi

if [[ -z "${PBS_FINGERPRINT:-}" ]]; then
    print_warn "PBS_FINGERPRINT missing in $ENV_FILE_SOURCE; proceeding without fingerprint pin check"
fi

namespace_args=()
if [[ -n "${NAMESPACE:-}" ]]; then
    namespace_args=(--ns "$NAMESPACE")
fi

print_sub "Searching PBS snapshots for backup id '$BACKUP_ID'..."
snapshot=$(proxmox-backup-client snapshots --repository "$REPOSITORY" "${namespace_args[@]}" 2>/dev/null | awk -F'│' -v backup_id="$BACKUP_ID" '
    NF >= 2 {
        snap=$2
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", snap)
        if (snap ~ ("^host/" backup_id "/")) {
            print snap
        }
    }
' | sort | tail -n 1)

if [[ -z "$snapshot" ]]; then
    print_sub "No PBS snapshot found for host/$BACKUP_ID; skipping restore"
    exit 0
fi

print_sub "Restoring snapshot $snapshot..."
rm -rf "$RESTORE_ROOT"
mkdir -p "$RESTORE_ROOT/etc-pve" "$RESTORE_ROOT/etc-ceph"
mkdir -p "$RESTORE_OUTPUT_DIR"

proxmox-backup-client restore "$snapshot" "${ARCHIVE_NAME}.pxar" "$RESTORE_ROOT/etc-pve" --repository "$REPOSITORY" "${namespace_args[@]}"

if [[ -d "$RESTORE_ROOT/etc-pve/etc/pve" ]]; then
    src_pve="$RESTORE_ROOT/etc-pve/etc/pve"
elif [[ -d "$RESTORE_ROOT/etc-pve" ]]; then
    src_pve="$RESTORE_ROOT/etc-pve"
else
    print_warn "Restored archive did not contain /etc/pve layout; skipping"
    exit 0
fi

rm -rf "$RESTORE_OUTPUT_DIR/latest"
cp -r "$src_pve" "$RESTORE_OUTPUT_DIR/latest"
print_sub "Fetched /etc/pve backup to $RESTORE_OUTPUT_DIR/latest"

apply_restored_notifications "$RESTORE_OUTPUT_DIR/latest"

if [[ "${CEPH_ENABLED:-false}" == "true" ]]; then
    if proxmox-backup-client restore "$snapshot" "etc-ceph.pxar" "$RESTORE_ROOT/etc-ceph" --repository "$REPOSITORY" "${namespace_args[@]}" >/dev/null 2>&1; then
        if [[ -d "$RESTORE_ROOT/etc-ceph/etc/ceph" ]]; then
            src_ceph="$RESTORE_ROOT/etc-ceph/etc/ceph"
        else
            src_ceph="$RESTORE_ROOT/etc-ceph"
        fi
        rm -rf "$RESTORE_OUTPUT_DIR/latest-ceph"
        cp -r "$src_ceph" "$RESTORE_OUTPUT_DIR/latest-ceph"
        print_sub "Fetched /etc/ceph backup to $RESTORE_OUTPUT_DIR/latest-ceph"
    else
        print_sub "No etc-ceph archive in snapshot; skipping /etc/ceph restore"
    fi
fi

print_sub "PBS config restore completed"
