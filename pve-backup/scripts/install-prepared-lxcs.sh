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

restore_one_ct() {
    local index="$1"
    local vmid storage target_storage unprivileged net0 start mount_count backup_volume mount_index mount_var mount_value

    vmid_var="RESTORE_CT_${index}_VMID"
    storage_var="RESTORE_CT_${index}_STORAGE"
    target_storage_var="RESTORE_CT_${index}_TARGET_STORAGE"
    unprivileged_var="RESTORE_CT_${index}_UNPRIVILEGED"
    net0_var="RESTORE_CT_${index}_NET0"
    start_var="RESTORE_CT_${index}_START"
    mount_count_var="RESTORE_CT_${index}_MOUNT_COUNT"

    vmid="${!vmid_var:-}"
    storage="${!storage_var:-}"
    target_storage="${!target_storage_var:-}"
    unprivileged="${!unprivileged_var:-}"
    net0="${!net0_var:-}"
    start="${!start_var:-false}"
    mount_count="${!mount_count_var:-0}"

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
        "${restore_args[@]}"
    fi

    if [[ -n "$net0" ]]; then
        print_sub "Setting LXC $vmid net0"
        pct set "$vmid" --net0 "$net0"
    fi

    for (( mount_index=0; mount_index<mount_count; mount_index++ )); do
        mount_var="RESTORE_CT_${index}_MOUNT_${mount_index}"
        mount_value="${!mount_var:-}"
        if [[ -n "$mount_value" ]]; then
            print_sub "Setting LXC $vmid mp${mount_index}"
            pct set "$vmid" "-mp${mount_index}" "$mount_value"
        fi
    done

    if [[ "$start" == "true" ]]; then
        print_sub "Starting LXC $vmid"
        pct start "$vmid" || print_warn "failed to start LXC $vmid"
    fi
}

count="${RESTORE_CT_COUNT:-0}"
for (( index=0; index<count; index++ )); do
    restore_one_ct "$index"
done
