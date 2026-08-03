#!/bin/bash
# install-pbs-config-restore.sh - Restore PVE config from PBS on fresh standalone installs

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/restore-plan.conf"
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
        print_error "No LXC configs listed for restore"
        return 1
    fi

    if [[ ! -d "$source_lxc_dir" ]]; then
        print_error "No restored LXC config directory found: $source_lxc_dir"
        return 1
    fi

    mkdir -p "$live_lxc_dir"

    for (( index=0; index<count; index++ )); do
        vmid_var="RESTORE_LXC_CONFIG_${index}_VMID"
        vmid="${!vmid_var:-}"
        if [[ ! "$vmid" =~ ^[1-9][0-9]{0,8}$ ]]; then
            print_error "Invalid LXC VMID in restore plan at index $index"
            return 1
        fi

        source_config="$source_lxc_dir/$vmid.conf"
        live_config="$live_lxc_dir/$vmid.conf"
        if [[ ! -f "$source_config" ]]; then
            print_error "Restored LXC config missing: $source_config"
            return 1
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

        warn_missing_lxc_volumes "$desired_config" "$vmid" || return 1
        copy_lxc_config "$desired_config" "$live_config"
        rm -f "$desired_config"
        print_sub "Restored LXC $vmid config"

        if [[ "${RESTORE_LXC_AUTOSTART:-false}" == "true" ]]; then
            pct start "$vmid" || return 1
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
            print_error "LXC $vmid references missing volume: $volume"
            return 1
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

if ! command -v proxmox-backup-client >/dev/null 2>&1; then
    print_error "proxmox-backup-client not found"
    exit 1
fi

# shellcheck disable=SC1090
source "$PLAN_FILE"

if [[ -z "${BACKUP_ID:-}" || -z "${ARCHIVE_NAME:-}" ]]; then
    print_error "Restore plan missing required values"
    exit 1
fi

# Client-side encryption: if the /etc/pve archive was written encrypted, the
# same keyfile must be supplied to decrypt on restore. ENCRYPT/KEYFILE come from
# the restore plan; the keyfile itself is installed by pbs-client-backup at KEYFILE.
crypt_args=()
if [[ "${ENCRYPT:-false}" == "true" ]]; then
    keyfile="${KEYFILE:-/etc/homelab/pbs-encryption.key}"
    if [[ ! -f "$keyfile" ]]; then
        print_error "Restore plan marks encryption but keyfile missing: $keyfile"
        exit 1
    fi
    crypt_args=(--keyfile "$keyfile")
fi

namespace_args=()
if [[ -n "${NAMESPACE:-}" ]]; then
    namespace_args=(--ns "$NAMESPACE")
fi

all_lxc_configs_present() {
    local count="${RESTORE_LXC_CONFIG_COUNT:-0}" index vmid
    [[ "${RESTORE_LXC_CONFIGS_ENABLED:-false}" != "true" ]] && return 0
    [[ "$count" =~ ^[0-9]+$ ]] || return 1
    for ((index = 0; index < count; index++)); do
        vmid_var="RESTORE_LXC_CONFIG_${index}_VMID"
        vmid="${!vmid_var:-}"
        [[ -f "/etc/pve/nodes/$HOST/lxc/$vmid.conf" ]] || return 1
    done
}

if [[ "${FORCE_UPDATE:-false}" != "true" ]] && all_lxc_configs_present; then
    print_sub "All requested LXC configs already exist; skipping PBS config restore"
    exit 0
fi

restore_from_destination() {
    local repository="$1" env_file="$2" snapshot
    if [[ ! -f "$env_file" ]]; then
        print_warn "Missing PBS env file: $env_file"
        return 1
    fi
    unset PBS_PASSWORD PBS_TOKEN_SECRET PBS_FINGERPRINT
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    if [[ -z "${PBS_PASSWORD:-}" ]]; then
        PBS_PASSWORD="${PBS_TOKEN_SECRET:-}"
    fi
    if [[ -z "${PBS_PASSWORD:-}" ]]; then
        print_warn "PBS credentials missing for $repository"
        return 1
    fi
    if [[ "$repository" == *'!'* && -z "${PBS_TOKEN_SECRET:-}" ]]; then PBS_TOKEN_SECRET="$PBS_PASSWORD"; fi
    export PBS_PASSWORD PBS_TOKEN_SECRET PBS_FINGERPRINT
    print_sub "Searching $repository for backup id '$BACKUP_ID'..."
    snapshot=$(proxmox-backup-client snapshots --repository "$repository" "${namespace_args[@]}" 2>/dev/null | awk -F'│' -v backup_id="$BACKUP_ID" '
    NF >= 2 {
        snap=$2
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", snap)
        if (snap ~ ("^host/" backup_id "/")) {
            print snap
        }
    }
' | sort | tail -n 1)
    if [[ -z "$snapshot" ]]; then
        print_warn "No PBS snapshot found for host/$BACKUP_ID on $repository"
        return 1
    fi
    print_sub "Restoring snapshot $snapshot from $repository..."
    rm -rf "$RESTORE_ROOT"
    mkdir -p "$RESTORE_ROOT/etc-pve"
    if ! proxmox-backup-client restore "$snapshot" "${ARCHIVE_NAME}.pxar" "$RESTORE_ROOT/etc-pve" --repository "$repository" "${namespace_args[@]}" "${crypt_args[@]}"; then
        print_warn "Restore failed from $repository"
        return 1
    fi
    return 0
}

rm -rf "$RESTORE_ROOT"
mkdir -p "$RESTORE_OUTPUT_DIR"
destination_count="${DESTINATION_COUNT:-0}"
if [[ ! "$destination_count" =~ ^[1-9][0-9]*$ ]]; then
    print_error "Restore plan has no PBS destinations"
    exit 1
fi
restored=false
for ((index = 0; index < destination_count; index++)); do
    repository_var="DESTINATION_${index}_REPOSITORY"
    repository="${!repository_var:-}"
    if [[ -n "$repository" ]] && restore_from_destination "$repository" "$BUILD_DIR/pbs-$index.env"; then
        restored=true
        break
    fi
done
if [[ "$restored" != true ]]; then
    print_error "No configured PBS destination restored host/$BACKUP_ID"
    exit 1
fi

if [[ -d "$RESTORE_ROOT/etc-pve/etc/pve" ]]; then
    src_pve="$RESTORE_ROOT/etc-pve/etc/pve"
elif [[ -d "$RESTORE_ROOT/etc-pve" ]]; then
    src_pve="$RESTORE_ROOT/etc-pve"
else
    print_error "Restored archive did not contain /etc/pve layout"
    exit 1
fi

rm -rf "$RESTORE_OUTPUT_DIR/latest"
cp -r "$src_pve" "$RESTORE_OUTPUT_DIR/latest"
print_sub "Fetched /etc/pve backup to $RESTORE_OUTPUT_DIR/latest"

apply_restored_notifications "$RESTORE_OUTPUT_DIR/latest"
restore_lxc_configs "$RESTORE_OUTPUT_DIR/latest" || exit 1

print_sub "PBS config restore completed"
