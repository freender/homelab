#!/usr/bin/env bash
set -uo pipefail

VMID=${1:-unknown}
PHASE=${2:-unknown}
BBOLT=/usr/local/bin/bbolt
LOG=/var/log/homelab-docker-bbolt-sync-hook.log
CONTAINERD_REL=var/lib/containerd
COPY_TIMEOUT=${BBOLT_HOOK_COPY_TIMEOUT:-30}
CHECK_TIMEOUT=${BBOLT_HOOK_CHECK_TIMEOUT:-30}

log() {
    local msg=$*
    printf '%s vmid=%s phase=%s node=%s %s\n' \
        "$(date -Is)" \
        "$VMID" \
        "$PHASE" \
        "$(hostname)" \
        "$msg" | tee -a "$LOG" | systemd-cat -t homelab-docker-bbolt-sync-hook -p info
}

finish() {
    # Diagnostic/sync only. Never block CT lifecycle.
    exit 0
}

notify_corruption() {
    local vmid=$1 phase=$2 db=$3 rc=$4 detail=$5
    local msg repair_cmd

    local env_locations=("/etc/homelab/telegram.env" "/etc/apcupsd/telegram/telegram.env")
    for f in "${env_locations[@]}"; do
        # shellcheck source=/dev/null
        [[ -f $f ]] && { source "$f"; break; } || true
    done

    [[ -n ${TELEGRAM_TOKEN:-} && -n ${TELEGRAM_CHATID:-} ]] || {
        log "notify=SKIP reason=no_telegram_creds vmid=$vmid phase=$phase db=$db"
        return 0
    }

    repair_cmd="homelab-docker-bbolt-repair.sh $vmid --yes --redeploy"

    msg="$(printf \
'⚠️ *containerd DB corrupt* — CT %s on %s
Phase: `%s`
DB: `%s`
bbolt rc: %s
%s

%s

Recommendation: run the manual repair on the current CT host. It stops Docker/containerd, re-checks all containerd bbolt DBs from stable copies, and only moves DBs that still fail.

Manual repair:
`%s`' \
        "$vmid" "$(hostname -s)" "$phase" "$db" "$rc" "$detail" "Auto-restore: disabled; manual action required." "$repair_cmd")"

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHATID}" \
        --data-urlencode "text=${msg}" \
        --data-urlencode "parse_mode=Markdown" \
        >/dev/null || true
}

check_db() {
    local db=$1
    local label=$2
    local size mtime sha_before tmp out sha_copy start rc elapsed detail

    size=$(stat -c %s "$db" 2>/dev/null || printf unknown)
    mtime=$(stat -c %y "$db" 2>/dev/null || printf unknown)
    sha_before=$(sha256sum "$db" 2>/dev/null | awk '{print $1}' || printf unknown)

    tmp=$(mktemp /tmp/homelab-docker-bbolt-sync-hook.XXXXXX.db) || {
        log "result=SKIP reason=mktemp_failed label=$label db=$db size=$size mtime=\"$mtime\" sha256=$sha_before"
        return 0
    }
    out=$(mktemp /tmp/homelab-docker-bbolt-sync-hook.XXXXXX.out) || {
        rm -f "$tmp"
        log "result=SKIP reason=mktemp_output_failed label=$label db=$db size=$size mtime=\"$mtime\" sha256=$sha_before"
        return 0
    }

    timeout "$COPY_TIMEOUT" cp --reflink=auto --sparse=always "$db" "$tmp" 2>"$out"
    rc=$?
    if [[ $rc -ne 0 ]]; then
        detail=$(tr '\n' ' ' <"$out" | cut -c1-700)
        log "result=SKIP step=copy rc=$rc copy_timeout=${COPY_TIMEOUT}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before detail=\"$detail\""
        rm -f "$tmp" "$out"
        return 0
    fi

    sha_copy=$(sha256sum "$tmp" 2>/dev/null | awk '{print $1}' || printf unknown)
    if [[ -x $BBOLT ]]; then
        start=$(date +%s)
        timeout "$CHECK_TIMEOUT" "$BBOLT" check "$tmp" >"$out" 2>&1
        rc=$?
        elapsed=$(( $(date +%s) - start ))
        detail=$(tr '\n' ' ' <"$out" | cut -c1-700)
        if [[ $rc -eq 0 ]]; then
            log "result=OK rc=$rc elapsed=${elapsed}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy"
        elif [[ $rc -eq 124 ]]; then
            log "result=SKIP reason=check_timeout rc=$rc check_timeout=${CHECK_TIMEOUT}s elapsed=${elapsed}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy detail=\"$detail\""
        else
            log "result=FAIL rc=$rc elapsed=${elapsed}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy detail=\"$detail\""
            notify_corruption "$VMID" "$PHASE" "$label" "$rc" "$detail"
        fi
    else
        log "result=SHA_ONLY reason=bbolt_missing label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy"
    fi

    rm -f "$tmp" "$out"
}

add_sync_path() {
    local path=$1
    local fsid existing_fsid

    [[ -e $path ]] || return 0
    fsid=$(stat -f -c '%d:%T' "$path" 2>/dev/null || true)
    [[ -n $fsid ]] || return 0
    for existing_fsid in "${sync_fsids[@]}"; do
        [[ $existing_fsid == "$fsid" ]] && return 0
    done
    sync_fsids+=("$fsid")
    sync_paths+=("$path")
}

collect_bbolt_dbs() {
    local db

    dbs=()
    shopt -s nullglob
    for db in \
        "$containerd_root"/io.containerd.*.bolt/*.db \
        "$containerd_root"/io.containerd.snapshotter.v1.*/*.db; do
        [[ -f $db ]] || continue
        dbs+=("$db")
    done
    shopt -u nullglob
}

rootfs_ref=$(pct config "$VMID" 2>/dev/null | awk -F': ' '/^rootfs:/{print $2; exit}' | cut -d, -f1)
if [[ -z ${rootfs_ref:-} ]]; then
    log "result=SKIP reason=rootfs_ref_missing"
    finish
fi

rootfs_path=$(pvesm path "$rootfs_ref" 2>/dev/null || true)
if [[ -z ${rootfs_path:-} || ! -d $rootfs_path ]]; then
    # ZFS subvolumes in this homelab are mounted at /<pool>/<subvol>.
    volume=${rootfs_ref#*:}
    pool=${rootfs_ref%%:*}
    rootfs_path="/${pool}/${volume}"
fi

containerd_root="${rootfs_path}/${CONTAINERD_REL}"
if [[ ! -d $containerd_root ]]; then
    log "result=SKIP reason=containerd_root_missing rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path containerd_root=$containerd_root"
    finish
fi

lock_file=/run/homelab-docker-bbolt-sync-hook.lock
if ! exec 9>"$lock_file"; then
    log "result=SKIP reason=lock_open_failed lock=$lock_file"
    finish
fi
if ! flock -n 9; then
    log "result=SKIP reason=already_running lock=$lock_file"
    finish
fi

sync_fsids=()
sync_paths=()
add_sync_path "$rootfs_path"
while IFS= read -r volume_ref; do
    volume_path=$(pvesm path "$volume_ref" 2>/dev/null || true)
    if [[ -z ${volume_path:-} || ! -e $volume_path ]]; then
        volume=${volume_ref#*:}
        pool=${volume_ref%%:*}
        volume_path="/${pool}/${volume}"
    fi
    add_sync_path "$volume_path"
done < <(pct config "$VMID" 2>/dev/null | awk -F': ' '/^mp[0-9]+:/{print $2}' | cut -d, -f1)

collect_bbolt_dbs

if [[ ${#dbs[@]} -eq 0 ]]; then
    log "result=SKIP reason=dbs_missing rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path containerd_root=$containerd_root"
    finish
fi

if [[ $PHASE == post-stop ]]; then
    # Filesystem sync is intentionally not performed here.
    # PVE/Replication.pm now calls syncfs() on every ZFS volume mountpoint
    # before taking the migration snapshot. The snapshot commit flushes the
    # resulting ZFS TXG, so no hookscript-level sync is needed here.
    log "sync=skipped reason=handled_by_replication_pm phase=$PHASE db_count=${#dbs[@]}"
fi

for db in "${dbs[@]}"; do
    label=${db#"$containerd_root"/}
    check_db "$db" "$label"
done

finish
