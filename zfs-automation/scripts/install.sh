#!/bin/bash

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

# shellcheck source=/dev/null
source "$BUILD_DIR/env"

HOMELAB_STATE_DIR="${HOMELAB_STATE_DIR:-/var/lib/homelab}"
MANAGED_DIR="${HOMELAB_STATE_DIR}/zfs-automation-managed"

cleanup_legacy_replication_units() {
    local path
    local unit_name

    LEGACY_REPLICATION_CLEANED=false

    for path in \
        /etc/systemd/system/homelab-zfs-replication.service \
        /etc/systemd/system/homelab-zfs-replication.timer \
        /usr/local/bin/homelab-zfs-replication \
        "$MANAGED_DIR/homelab-zfs-replication.service" \
        "$MANAGED_DIR/homelab-zfs-replication.timer" \
        "$MANAGED_DIR/homelab-zfs-replication.sh"; do
        [[ -e "$path" ]] || continue
        unit_name="$(basename "$path")"
        if [[ "$unit_name" == "homelab-zfs-replication.timer" ]] && systemctl is-enabled --quiet "$unit_name" 2>/dev/null; then
            systemctl disable --now "$unit_name"
        fi
        rm -f "$path"
        LEGACY_REPLICATION_CLEANED=true
        print_ok "Removed legacy $unit_name"
    done
}

cleanup_obsolete_replication_units() {
    local path
    local unit_name
    local helper_name

    OBSOLETE_REPLICATION_CLEANED=false

    shopt -s nullglob
    for path in \
        /etc/systemd/system/homelab-zfs-replication-*.service \
        /etc/systemd/system/homelab-zfs-replication-*.timer \
        /usr/local/bin/homelab-zfs-replication-* \
        "$MANAGED_DIR"/homelab-zfs-replication-*.service \
        "$MANAGED_DIR"/homelab-zfs-replication-*.timer \
        "$MANAGED_DIR"/homelab-zfs-replication-*.sh; do
        unit_name="$(basename "$path")"
        helper_name="$unit_name"
        if [[ -n "${FILE_MAP_DEST[$helper_name]:-}" ]]; then
            continue
        fi
        if [[ "$unit_name" == homelab-zfs-replication-* && "$unit_name" != *.service && "$unit_name" != *.timer && "$unit_name" != *.sh ]]; then
            helper_name="${unit_name}.sh"
            [[ -n "${FILE_MAP_DEST[$helper_name]:-}" ]] && continue
        fi

        if [[ "$unit_name" == homelab-zfs-replication-*.timer ]] && systemctl is-enabled --quiet "$unit_name" 2>/dev/null; then
            systemctl disable --now "$unit_name"
        fi

        rm -f "$path"
        OBSOLETE_REPLICATION_CLEANED=true
        print_ok "Removed obsolete $unit_name"
    done
    shopt -u nullglob
}

cleanup_legacy_rebuild_bundle() {
    local legacy_root="${HOMELAB_STATE_DIR}/zfs-automation"

    if [[ -d "$legacy_root" ]]; then
        rm -rf "$legacy_root"
        print_ok "Removed legacy local rebuild bundle at $legacy_root"
    fi
}

prepare_zfs_pull_source_user() {
    if [[ "${ENABLE_ZFS_PULL_SOURCE:-false}" != "true" ]]; then
        return 0
    fi

    if ! id "$ZFS_PULL_SOURCE_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$ZFS_PULL_SOURCE_HOME" --create-home --shell /bin/bash "$ZFS_PULL_SOURCE_USER"
        print_ok "Created $ZFS_PULL_SOURCE_USER user"
    fi

    mkdir -p "$ZFS_PULL_SOURCE_HOME/.ssh" /etc/homelab
    chmod 700 "$ZFS_PULL_SOURCE_HOME/.ssh"
    chown -R "$ZFS_PULL_SOURCE_USER:$ZFS_PULL_SOURCE_USER" "$ZFS_PULL_SOURCE_HOME"
}

prepare_zfs_push_target_user() {
    if [[ "${ENABLE_ZFS_PUSH_TARGET:-false}" != "true" ]]; then
        return 0
    fi

    if ! id "$ZFS_PUSH_TARGET_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$ZFS_PUSH_TARGET_HOME" --create-home --shell /bin/bash "$ZFS_PUSH_TARGET_USER"
        print_ok "Created $ZFS_PUSH_TARGET_USER user"
    fi

    mkdir -p "$ZFS_PUSH_TARGET_HOME/.ssh" /etc/homelab
    chmod 700 "$ZFS_PUSH_TARGET_HOME/.ssh"
    chown -R "$ZFS_PUSH_TARGET_USER:$ZFS_PUSH_TARGET_USER" "$ZFS_PUSH_TARGET_HOME"
}

cleanup_zfs_pull_source_access() {
    local path

    if [[ "${ENABLE_ZFS_PULL_SOURCE:-false}" == "true" ]]; then
        return 0
    fi

    for path in \
        /etc/homelab/zfs-pull-datasets.conf \
        /usr/local/sbin/homelab-zfs-send-only \
        "$ZFS_PULL_SOURCE_HOME/.ssh/authorized_keys" \
        "$MANAGED_DIR/homelab-zfs-send-only.sh" \
        "$MANAGED_DIR/zfs-pull-authorized-keys" \
        "$MANAGED_DIR/zfs-pull-datasets.conf"; do
        [[ -e "$path" ]] || continue
        rm -f "$path"
        print_ok "Removed obsolete $(basename "$path")"
    done
}

cleanup_zfs_push_target_access() {
    local path

    if [[ "${ENABLE_ZFS_PUSH_TARGET:-false}" == "true" ]]; then
        return 0
    fi

    for path in \
        /etc/homelab/zfs-push-datasets.conf \
        /usr/local/sbin/homelab-zfs-receive-only \
        "$ZFS_PUSH_TARGET_HOME/.ssh/authorized_keys" \
        "$MANAGED_DIR/homelab-zfs-receive-only.sh" \
        "$MANAGED_DIR/zfs-push-authorized-keys" \
        "$MANAGED_DIR/zfs-push-datasets.conf"; do
        [[ -e "$path" ]] || continue
        rm -f "$path"
        print_ok "Removed obsolete $(basename "$path")"
    done
}

configure_zfs_pull_source_access() {
    local dataset

    if [[ "${ENABLE_ZFS_PULL_SOURCE:-false}" != "true" ]]; then
        return 0
    fi

    require_file /etc/homelab/zfs-pull-datasets.conf /etc/homelab/zfs-pull-datasets.conf || exit 1
    require_file "$ZFS_PULL_SOURCE_HOME/.ssh/authorized_keys" "$ZFS_PULL_SOURCE_HOME/.ssh/authorized_keys" || exit 1
    require_file /usr/local/sbin/homelab-zfs-send-only /usr/local/sbin/homelab-zfs-send-only || exit 1

    chown -R "$ZFS_PULL_SOURCE_USER:$ZFS_PULL_SOURCE_USER" "$ZFS_PULL_SOURCE_HOME"
    chmod 700 "$ZFS_PULL_SOURCE_HOME/.ssh"
    chmod 600 "$ZFS_PULL_SOURCE_HOME/.ssh/authorized_keys"
    chmod 755 /usr/local/sbin/homelab-zfs-send-only

    while IFS= read -r dataset; do
        [[ -n "$dataset" ]] || continue
        grant_zfs_pull_source_dataset "$dataset"
    done < /etc/homelab/zfs-pull-datasets.conf
}

configure_zfs_push_target_access() {
    local dataset

    if [[ "${ENABLE_ZFS_PUSH_TARGET:-false}" != "true" ]]; then
        return 0
    fi

    require_file /etc/homelab/zfs-push-datasets.conf /etc/homelab/zfs-push-datasets.conf || exit 1
    require_file "$ZFS_PUSH_TARGET_HOME/.ssh/authorized_keys" "$ZFS_PUSH_TARGET_HOME/.ssh/authorized_keys" || exit 1
    require_file /usr/local/sbin/homelab-zfs-receive-only /usr/local/sbin/homelab-zfs-receive-only || exit 1

    chown -R "$ZFS_PUSH_TARGET_USER:$ZFS_PUSH_TARGET_USER" "$ZFS_PUSH_TARGET_HOME"
    chmod 700 "$ZFS_PUSH_TARGET_HOME/.ssh"
    chmod 600 "$ZFS_PUSH_TARGET_HOME/.ssh/authorized_keys"
    chmod 755 /usr/local/sbin/homelab-zfs-receive-only

    while IFS= read -r dataset; do
        [[ -n "$dataset" ]] || continue
        grant_zfs_push_target_dataset "$dataset"
    done < /etc/homelab/zfs-push-datasets.conf
}

grant_zfs_pull_source_dataset() {
    local dataset="$1"
    local parent="$dataset"

    if zfs list -H -o name "$dataset" >/dev/null 2>&1; then
        zfs allow -u "$ZFS_PULL_SOURCE_USER" send,hold,release "$dataset"
        print_ok "Granted send-only pull access for $dataset"
        return 0
    fi

    while [[ "$parent" == */* ]]; do
        parent="${parent%/*}"
        if zfs list -H -o name "$parent" >/dev/null 2>&1; then
            zfs allow -d -u "$ZFS_PULL_SOURCE_USER" send,hold,release "$parent"
            print_warn "ZFS pull source dataset not present yet: $dataset; granted future descendant access at $parent"
            return 0
        fi
    done

    print_error "ZFS pull source dataset parent not found: $dataset"
    exit 1
}

grant_zfs_push_target_dataset() {
    local dataset="$1"
    local parent="$dataset"

    if zfs list -H -o name "$dataset" >/dev/null 2>&1; then
        zfs allow -u "$ZFS_PUSH_TARGET_USER" create,mount,receive,hold,release "$dataset"
        print_ok "Granted receive target access for $dataset"
        return 0
    fi

    while [[ "$parent" == */* ]]; do
        parent="${parent%/*}"
        if zfs list -H -o name "$parent" >/dev/null 2>&1; then
            zfs allow -d -u "$ZFS_PUSH_TARGET_USER" create,mount,receive,hold,release "$parent"
            print_warn "ZFS push target dataset not present yet: $dataset; granted future descendant access at $parent"
            return 0
        fi
    done

    print_error "ZFS push target dataset parent not found: $dataset"
    exit 1
}

load_file_map

print_header "ZFS Automation"

print_action "Legacy local rebuild bundle"
cleanup_legacy_rebuild_bundle

print_action "TRIM"
if ! systemctl is-enabled --quiet fstrim.timer 2>/dev/null; then
    systemctl enable --now fstrim.timer
    print_ok "fstrim.timer enabled"
else
    print_sub "fstrim.timer already enabled"
fi

print_action "Sanoid / Syncoid"
if ! command -v sanoid >/dev/null 2>&1 \
    || ! command -v syncoid >/dev/null 2>&1 \
    || ! command -v lzop >/dev/null 2>&1 \
    || ! command -v mbuffer >/dev/null 2>&1 \
    || ! command -v pv >/dev/null 2>&1; then
    apt-get install -y -q sanoid lzop mbuffer pv
    print_ok "Sanoid/Syncoid helper packages installed"
else
    print_sub "Sanoid/Syncoid helper packages already installed"
fi

mkdir -p /etc/sanoid "$HOMELAB_STATE_DIR" "$MANAGED_DIR"
prepare_zfs_pull_source_user
prepare_zfs_push_target_user
cleanup_zfs_pull_source_access
cleanup_zfs_push_target_access

cleanup_legacy_replication_units
cleanup_obsolete_replication_units

rc=0
install_build_file "sanoid.conf" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    print_ok "sanoid.conf updated"
fi

for helper in "${!FILE_MAP_DEST[@]}"; do
    rc=0
    install_if_changed "$BUILD_DIR/$helper" "$MANAGED_DIR/$helper" "$(mapped_mode "$helper")" "$MANAGED_DIR/$helper" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
done

units_changed=false
[[ "$LEGACY_REPLICATION_CLEANED" == "true" || "$OBSOLETE_REPLICATION_CLEANED" == "true" ]] && units_changed=true
for unit in "${!FILE_MAP_DEST[@]}"; do
    if [[ "$unit" == "sanoid.conf" ]]; then
        continue
    fi
    rc=0
    install_build_file "$unit" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && units_changed=true
done

configure_zfs_pull_source_access
configure_zfs_push_target_access

if systemctl is-enabled --quiet sanoid.timer 2>/dev/null; then
    systemctl disable --now sanoid.timer
    print_ok "Disabled packaged sanoid.timer"
fi

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

ensure_timer_state homelab-zfs-snapshots.timer "$ENABLE_ZFS_SNAPSHOTS" "$units_changed"

for unit in "${!FILE_MAP_DEST[@]}"; do
    if [[ "$unit" == homelab-zfs-replication-*.timer ]]; then
        ensure_timer_state "$unit" "$ENABLE_ZFS_REPLICATION" "$units_changed"
    fi
done

ensure_timer_state zfs-scrub.timer "$ENABLE_ZFS_SCRUB" "$units_changed"
ensure_timer_state homelab-zfs-health-check.timer "$ENABLE_ZFS_HEALTH_CHECK" "$units_changed"

print_header "ZFS Automation Complete"
