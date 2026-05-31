#!/usr/bin/env bash
set -uo pipefail

VMID=${1:-unknown}
PHASE=${2:-unknown}
BBOLT=/usr/local/bin/bbolt
LOG=/var/log/homelab-docker-bbolt-sync-hook.log
CONTAINERD_REL=var/lib/containerd

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
    # Diagnostic/sync only. Never block CT lifecycle while this is under test.
    exit 0
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

    if ! cp --reflink=auto --sparse=always "$db" "$tmp" 2>"$out"; then
        detail=$(tr '\n' ' ' <"$out" | cut -c1-700)
        log "result=FAIL step=copy label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before detail=\"$detail\""
        rm -f "$tmp" "$out"
        return 0
    fi

    sha_copy=$(sha256sum "$tmp" 2>/dev/null | awk '{print $1}' || printf unknown)
    if [[ -x $BBOLT ]]; then
        start=$(date +%s)
        timeout 120 "$BBOLT" check "$tmp" >"$out" 2>&1
        rc=$?
        elapsed=$(( $(date +%s) - start ))
        detail=$(tr '\n' ' ' <"$out" | cut -c1-700)
        if [[ $rc -eq 0 ]]; then
            log "result=OK rc=$rc elapsed=${elapsed}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy"
        else
            log "result=FAIL rc=$rc elapsed=${elapsed}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy detail=\"$detail\""
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

dbs=()
for db in \
    "$containerd_root"/io.containerd.metadata.v1.bolt/meta.db \
    "$containerd_root"/io.containerd.mount-manager.v1.bolt/meta.db \
    "$containerd_root"/io.containerd.snapshotter.v1.*/metadata.db; do
    [[ -f $db ]] || continue
    dbs+=("$db")
done

if [[ ${#dbs[@]} -eq 0 ]]; then
    log "result=SKIP reason=dbs_missing rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path containerd_root=$containerd_root"
    finish
fi

if [[ $PHASE == post-stop ]]; then
    start_sync=$(date +%s)
    for sync_path in "${sync_paths[@]}"; do
        sync -f "$sync_path" 2>/dev/null || sync
    done
    if command -v zpool >/dev/null 2>&1; then
        pool_name=${rootfs_ref%%:*}
        zpool sync "$pool_name" 2>/dev/null || true
    fi
    log "sync=done mode=filesystem elapsed=$(( $(date +%s) - start_sync ))s fs_count=${#sync_paths[@]} db_count=${#dbs[@]} sync_paths=\"${sync_paths[*]}\" containerd_root=$containerd_root"
fi

for db in "${dbs[@]}"; do
    label=${db#"$containerd_root"/}
    check_db "$db" "$label"
done

finish
