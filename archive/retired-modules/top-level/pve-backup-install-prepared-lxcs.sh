#!/bin/bash
# Restore prepared LXC backups from configured PBS storage and apply final config.

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/restore-ct-plan.conf"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    Warning: $*"; }
fi

if [[ ! -f "$PLAN_FILE" ]]; then
    print_sub "Prepared LXC restore plan not present; skipping"
    exit 0
fi

# shellcheck disable=SC1090
source "$PLAN_FILE"

find_latest_backup_volume() {
    local storage="$1"
    local vmid="$2"

    pvesm list "$storage" --vmid "$vmid" 2>/dev/null \
        | awk -v vmid="$vmid" '$1 ~ ("backup/ct/" vmid "/") { print $1 }' \
        | sort \
        | tail -n 1
}

install_root_authorized_keys() {
    local vmid="$1"
    local key_count="$2"
    local key_index key_var public_key

    if [[ -z "$key_count" || "$key_count" == "0" ]]; then
        return 0
    fi
    if ! pct status "$vmid" | grep -q "status: running"; then
        print_warn "LXC $vmid is not running; cannot install root SSH keys"
        return 0
    fi

    print_sub "Installing root SSH keys in LXC $vmid"
    pct exec "$vmid" -- mkdir -p /root/.ssh
    pct exec "$vmid" -- chmod 700 /root/.ssh
    pct exec "$vmid" -- touch /root/.ssh/authorized_keys
    pct exec "$vmid" -- chmod 600 /root/.ssh/authorized_keys

    for (( key_index=0; key_index<key_count; key_index++ )); do
        key_var="RESTORE_CT_${index}_ROOT_AUTHORIZED_KEY_${key_index}"
        public_key="${!key_var:-}"
        if [[ -n "$public_key" ]]; then
            pct exec "$vmid" -- grep -qxF "$public_key" /root/.ssh/authorized_keys \
                || pct exec "$vmid" -- sh -c 'printf "%s\n" "$1" >> /root/.ssh/authorized_keys' sh "$public_key"
        fi
    done
}

restore_one_ct() {
    local index="$1"
    local vmid storage target_storage unprivileged ignore_unpack_errors start root_authorized_key_count backup_volume

    vmid_var="RESTORE_CT_${index}_VMID"
    storage_var="RESTORE_CT_${index}_STORAGE"
    target_storage_var="RESTORE_CT_${index}_TARGET_STORAGE"
    unprivileged_var="RESTORE_CT_${index}_UNPRIVILEGED"
    ignore_unpack_errors_var="RESTORE_CT_${index}_IGNORE_UNPACK_ERRORS"
    start_var="RESTORE_CT_${index}_START"
    root_authorized_key_count_var="RESTORE_CT_${index}_ROOT_AUTHORIZED_KEY_COUNT"

    vmid="${!vmid_var:-}"
    storage="${!storage_var:-}"
    target_storage="${!target_storage_var:-}"
    unprivileged="${!unprivileged_var:-}"
    ignore_unpack_errors="${!ignore_unpack_errors_var:-false}"
    start="${!start_var:-false}"
    root_authorized_key_count="${!root_authorized_key_count_var:-0}"

    if pct status "$vmid" >/dev/null 2>&1; then
        print_sub "LXC $vmid already exists; skipping restore"
    else
        backup_volume="$(find_latest_backup_volume "$storage" "$vmid")"
        if [[ -z "$backup_volume" ]]; then
            print_warn "No backup found on $storage for ct/$vmid; skipping"
            return 0
        fi
        print_sub "Restoring LXC $vmid from $backup_volume to $target_storage"
        restore_args=(pct restore "$vmid" "$backup_volume" --storage "$target_storage")
        if [[ -n "$unprivileged" ]]; then
            restore_args+=(--unprivileged "$unprivileged")
        fi
        if [[ "$ignore_unpack_errors" == "true" ]]; then
            restore_args+=(--ignore-unpack-errors 1)
        fi
        "${restore_args[@]}"
    fi

    if [[ "$start" == "true" ]]; then
        if pct status "$vmid" | grep -q "status: running"; then
            print_sub "LXC $vmid already running"
        else
            print_sub "Starting LXC $vmid"
            pct start "$vmid" || print_warn "failed to start LXC $vmid"
        fi
    fi

    install_root_authorized_keys "$vmid" "$root_authorized_key_count"
}

count="${RESTORE_CT_COUNT:-0}"
for (( index=0; index<count; index++ )); do
    restore_one_ct "$index"
done
