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

restore_lxc_configs() {
    local restored_root="$1"
    local count="${RESTORE_LXC_CONFIG_COUNT:-0}"
    local live_lxc_dir="/etc/pve/nodes/$HOST/lxc"
    local source_lxc_dir="$restored_root/nodes/$HOST/lxc"
    local index
    local vmid_var
    local vmid
    local source_config
    local live_config
    local desired_config

    if [[ "${RESTORE_LXC_CONFIGS_ENABLED:-false}" != "true" ]]; then
        print_sub "LXC config restore not enabled; skipping"
        return 0
    fi

    if [[ ! "$count" =~ ^[0-9]+$ || "$count" -eq 0 ]]; then
        print_sub "No LXC configs listed for restore; skipping"
        return 0
    fi

    if [[ ! -d "$source_lxc_dir" ]]; then
        print_warn "No restored LXC config directory found: $source_lxc_dir"
        return 0
    fi

    mkdir -p "$live_lxc_dir"

    for (( index=0; index<count; index++ )); do
        vmid_var="RESTORE_LXC_CONFIG_${index}_VMID"
        vmid="${!vmid_var:-}"
        if [[ ! "$vmid" =~ ^[1-9][0-9]{0,8}$ ]]; then
            print_warn "Invalid LXC VMID in restore plan at index $index; skipping"
            continue
        fi

        source_config="$source_lxc_dir/$vmid.conf"
        live_config="$live_lxc_dir/$vmid.conf"
        if [[ ! -f "$source_config" ]]; then
            print_warn "Restored LXC config missing: $source_config"
            continue
        fi

        desired_config="$(prepare_lxc_config "$source_config")"

        if [[ -f "$live_config" && "$FORCE_UPDATE" != "true" ]]; then
            if cmp -s "$desired_config" "$live_config"; then
                print_sub "LXC $vmid config already restored"
            else
                print_warn "LXC $vmid config exists; rerun with --force to overwrite"
            fi
            rm -f "$desired_config"
            continue
        fi

        warn_missing_lxc_volumes "$desired_config" "$vmid"
        copy_lxc_config "$desired_config" "$live_config"
        rm -f "$desired_config"
        print_sub "Restored LXC $vmid config"

        if [[ "${RESTORE_LXC_AUTOSTART:-false}" == "true" ]]; then
            pct start "$vmid" || print_warn "failed to start LXC $vmid"
        fi
    done
}

warn_missing_lxc_volumes() {
    local config="$1"
    local vmid="$2"
    local key
    local value
    local volume

    while IFS=: read -r key value; do
        case "$key" in
            rootfs|mp[0-9]*) ;;
            *) continue ;;
        esac
        value="${value# }"
        volume="${value%%,*}"
        if [[ "$volume" == *":"* ]] && ! pvesm path "$volume" >/dev/null 2>&1; then
            print_warn "LXC $vmid references missing volume: $volume"
        fi
    done < "$config"
}

prepare_lxc_config() {
    local source_config="$1"
    local tmp_config

    tmp_config="$(mktemp)"
    cp "$source_config" "$tmp_config"
    if [[ "${RESTORE_LXC_AUTOSTART:-false}" != "true" ]]; then
        if grep -q '^onboot:' "$tmp_config"; then
            sed -i 's/^onboot:.*/onboot: 0/' "$tmp_config"
        else
            printf '\nonboot: 0\n' >> "$tmp_config"
        fi
    fi

    printf '%s\n' "$tmp_config"
}

copy_lxc_config() {
    local source_config="$1"
    local live_config="$2"

    cp "$source_config" "$live_config"
    chown root:www-data "$live_config" 2>/dev/null || chown root:root "$live_config"
    chmod 640 "$live_config"
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
    if [[ "${REPOSITORY:-}" == *'!'* && -n "${PBS_TOKEN_SECRET:-}" ]]; then
        PBS_PASSWORD="$PBS_TOKEN_SECRET"
    fi
fi

if [[ -z "${PBS_PASSWORD:-}" ]]; then
    print_warn "PBS_PASSWORD missing in $ENV_FILE_SOURCE; skipping"
    exit 0
fi

if [[ "${REPOSITORY:-}" == *'!'* && -z "${PBS_TOKEN_SECRET:-}" ]]; then
    PBS_TOKEN_SECRET="$PBS_PASSWORD"
fi

export PBS_PASSWORD PBS_TOKEN_SECRET PBS_FINGERPRINT

if [[ -z "${PBS_FINGERPRINT:-}" ]]; then
    print_warn "PBS_FINGERPRINT missing in $ENV_FILE_SOURCE; proceeding without fingerprint pin check"
fi

# Client-side encryption: if the /etc/pve archive was written encrypted, the
# same keyfile must be supplied to decrypt on restore. ENCRYPT/KEYFILE come from
# the restore plan; the keyfile itself is installed by pbs-client-backup at KEYFILE.
crypt_args=()
if [[ "${ENCRYPT:-false}" == "true" ]]; then
    keyfile="${KEYFILE:-/etc/homelab/pbs-encryption.key}"
    if [[ ! -f "$keyfile" ]]; then
        print_warn "Restore plan marks encryption but keyfile missing: $keyfile; skipping restore"
        exit 0
    fi
    crypt_args=(--keyfile "$keyfile")
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

proxmox-backup-client restore "$snapshot" "${ARCHIVE_NAME}.pxar" "$RESTORE_ROOT/etc-pve" --repository "$REPOSITORY" "${namespace_args[@]}" "${crypt_args[@]}"

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
restore_lxc_configs "$RESTORE_OUTPUT_DIR/latest"

if [[ "${CEPH_ENABLED:-false}" == "true" ]]; then
    if proxmox-backup-client restore "$snapshot" "etc-ceph.pxar" "$RESTORE_ROOT/etc-ceph" --repository "$REPOSITORY" "${namespace_args[@]}" "${crypt_args[@]}" >/dev/null 2>&1; then
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
