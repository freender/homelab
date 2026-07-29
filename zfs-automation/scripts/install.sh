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

# ensure_timer_state treats anything != "true" as "disable", so an empty flag from a
# truncated env file would silently stop snapshots, scrub and replication rather
# than failing. Refuse to run on an ambiguous config.
require_env \
    ENABLE_ZFS_SNAPSHOTS \
    ENABLE_ZFS_SCRUB \
    ENABLE_ZFS_REPLICATION \
    || exit 1

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

# Retired: homelab-zfs-health-check was a strict subset of the pve-exporters
# homelab_zpool_healthy textfile metric (same "health column != ONLINE" check,
# 60x the latency, and unlike the exporter it can't detect an un-imported pool
# at all since `zpool list` only reports pools that actually imported). Cleans
# up wherever it was previously installed; safe to run even when it was never
# deployed here.
cleanup_retired_health_check() {
    local path

    RETIRED_HEALTH_CHECK_CLEANED=false

    if systemctl is-enabled --quiet homelab-zfs-health-check.timer 2>/dev/null; then
        systemctl disable --now homelab-zfs-health-check.timer
    fi
    systemctl stop homelab-zfs-health-check.service 2>/dev/null || true
    systemctl reset-failed homelab-zfs-health-check.service homelab-zfs-health-check.timer 2>/dev/null || true

    for path in \
        /etc/systemd/system/homelab-zfs-health-check.service \
        /etc/systemd/system/homelab-zfs-health-check.timer \
        /usr/local/bin/homelab-zfs-health-check \
        "$MANAGED_DIR/homelab-zfs-health-check.service" \
        "$MANAGED_DIR/homelab-zfs-health-check.timer" \
        "$MANAGED_DIR/homelab-zfs-health-check.sh"; do
        [[ -e "$path" ]] || continue
        rm -f "$path"
        RETIRED_HEALTH_CHECK_CLEANED=true
        print_ok "Removed retired $(basename "$path")"
    done
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
cleanup_retired_health_check

rc=0
install_build_file "sanoid.conf" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
if [[ $rc -eq 0 ]]; then
    print_ok "sanoid.conf updated"
fi

for helper in "${!FILE_MAP_DEST[@]}"; do
    if [[ "$helper" == source-private-key-* ]]; then
        continue
    fi
    rc=0
    install_if_changed "$BUILD_DIR/$helper" "$MANAGED_DIR/$helper" "$(mapped_mode "$helper")" "$MANAGED_DIR/$helper" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
done

units_changed=false
[[ "$LEGACY_REPLICATION_CLEANED" == "true" || "$OBSOLETE_REPLICATION_CLEANED" == "true" \
    || "$RETIRED_HEALTH_CHECK_CLEANED" == "true" ]] && units_changed=true
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

if [[ -x /usr/local/sbin/homelab-zfs-refresh-known-hosts ]]; then
    print_action "SSH known_hosts refresh"
    /usr/local/sbin/homelab-zfs-refresh-known-hosts
fi

if systemctl is-enabled --quiet sanoid.timer 2>/dev/null; then
    systemctl disable --now sanoid.timer
    print_ok "Disabled packaged sanoid.timer"
fi

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

# Paused: keep every unit installed and current, but stop and disable ALL
# managed zfs timers (snapshots, scrub, and every replication job) so no zfs
# automation runs. This is a host-wide freeze that overrides the per-area
# ENABLE_ZFS_* flags. Flip zfs-automation.paused back to false to resume.
# Replication jobs are enumerated from disk (not just the file map) so jobs
# that are still generated but paused are all caught.
if [[ "${PAUSED:-false}" == "true" ]]; then
    pause_units=(
        homelab-zfs-snapshots.timer
        zfs-scrub.timer
    )
    shopt -s nullglob
    for timer_path in /etc/systemd/system/homelab-zfs-replication-*.timer; do
        pause_units+=("$(basename "$timer_path")")
    done
    shopt -u nullglob

    homelab_apply_pause "true" "${pause_units[@]}"
    print_header "ZFS Automation Complete (paused)"
    exit 0
fi

ensure_timer_state homelab-zfs-snapshots.timer "$ENABLE_ZFS_SNAPSHOTS" "$units_changed"

# Per-job replication pause: a paused job keeps its units installed but its
# timer is stopped/disabled, while non-paused jobs follow ENABLE_ZFS_REPLICATION.
# Distinct from a retired job (enabled: false), whose units are removed above.
declare -A PAUSED_REPLICATION_TIMER_SET=()
for paused_timer in ${PAUSED_REPLICATION_TIMERS:-}; do
    PAUSED_REPLICATION_TIMER_SET["$paused_timer"]=1
done

replication_job_paused() {
    local unit="$1"
    [[ -n "${PAUSED_REPLICATION_TIMER_SET[$unit]:-}" ]]
}

for unit in "${!FILE_MAP_DEST[@]}"; do
    if [[ "$unit" == homelab-zfs-replication-*.timer ]]; then
        if replication_job_paused "$unit"; then
            ensure_timer_state "$unit" "false" "$units_changed"
        else
            ensure_timer_state "$unit" "$ENABLE_ZFS_REPLICATION" "$units_changed"
        fi
    fi
done

if [[ "${ZFS_REPLICATION_RECOVERY_START_FAILED:-false}" == "true" && "$ENABLE_ZFS_REPLICATION" == "true" ]]; then
    print_action "ZFS replication recovery"
    for unit in "${!FILE_MAP_DEST[@]}"; do
        if [[ "$unit" == homelab-zfs-replication-*.service ]] && systemctl is-failed --quiet "$unit"; then
            timer_unit="${unit%.service}.timer"
            if replication_job_paused "$timer_unit"; then
                print_sub "Skipping paused $unit"
                continue
            fi
            print_sub "Restarting failed $unit"
            systemctl reset-failed "$unit"
            systemctl start "$unit"
        fi
    done
fi

ensure_timer_state zfs-scrub.timer "$ENABLE_ZFS_SCRUB" "$units_changed"

print_header "ZFS Automation Complete"
