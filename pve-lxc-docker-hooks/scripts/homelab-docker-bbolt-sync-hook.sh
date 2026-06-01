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
    # Diagnostic/sync only. Never block CT lifecycle.
    exit 0
}

cluster_nodes() {
    pvecm nodes 2>/dev/null | awk 'NR > 1 {print $3}' || true
}

peer_clean_copy() {
    local db=$1
    local node remote_cmd peer_out peer_node peer_sha peer_mtime

    for node in $(cluster_nodes); do
        [[ -n $node ]] || continue
        [[ $node == "$(hostname -s)" ]] && continue

        remote_cmd=$(cat <<EOF
DB='$db'
BBOLT='$BBOLT'
TMP=\$(mktemp /tmp/homelab-peer-bbolt.XXXXXX.db) || exit 1
OUT=\$(mktemp /tmp/homelab-peer-bbolt.XXXXXX.out) || { rm -f "\$TMP"; exit 1; }
[[ -f \$DB ]] || { rm -f "\$TMP" "\$OUT"; exit 2; }
cp --reflink=auto --sparse=always "\$DB" "\$TMP" 2>/dev/null || { rm -f "\$TMP" "\$OUT"; exit 3; }
timeout 120 "\$BBOLT" check "\$TMP" >"\$OUT" 2>&1
RC=\$?
if [[ \$RC -eq 0 ]]; then
    SHA=\$(sha256sum "\$DB" 2>/dev/null | awk '{print \$1}')
    MTIME=\$(stat -c %y "\$DB" 2>/dev/null)
    printf 'OK %s %s\n' "\$SHA" "\$MTIME"
fi
rm -f "\$TMP" "\$OUT"
exit \$RC
EOF
)

        peer_out=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$node" "$remote_cmd" 2>/dev/null || true)
        [[ $peer_out == OK* ]] || continue

        peer_node=$node
        peer_sha=$(printf '%s\n' "$peer_out" | awk '{print $2}')
        peer_mtime=$(printf '%s\n' "$peer_out" | cut -d' ' -f3-)
        printf '%s|%s|%s\n' "$peer_node" "$peer_sha" "$peer_mtime"
        return 0
    done

    return 1
}

notify_corruption() {
    local vmid=$1 phase=$2 db=$3 rc=$4 detail=$5 auto_restore_note
    local msg latest_backup restore_cmd peer_copy peer_node peer_sha peer_mtime peer_restore_cmd

    auto_restore_note=${6:-}

    local env_locations=("/etc/homelab/telegram.env" "/etc/apcupsd/telegram/telegram.env")
    for f in "${env_locations[@]}"; do
        # shellcheck source=/dev/null
        [[ -f $f ]] && { source "$f"; break; } || true
    done

    [[ -n ${TELEGRAM_TOKEN:-} && -n ${TELEGRAM_CHATID:-} ]] || {
        log "notify=SKIP reason=no_telegram_creds vmid=$vmid phase=$phase db=$db"
        return 0
    }

    latest_backup=$(pvesm list backup-main 2>/dev/null \
        | awk -v vmid="$vmid" '$0 ~ "ct/"vmid"/" {print $1}' \
        | sort | tail -1 || true)
    if [[ -n $latest_backup ]]; then
        restore_cmd="pct restore $vmid $latest_backup --storage vm-flash"
    else
        restore_cmd="(no recent PBS snapshot found for CT $vmid)"
    fi

    peer_copy=$(peer_clean_copy "$rootfs_path/$CONTAINERD_REL/$db" || true)
    if [[ -n $peer_copy ]]; then
        IFS='|' read -r peer_node peer_sha peer_mtime <<<"$peer_copy"
        peer_restore_cmd="pct stop $vmid && scp root@${peer_node}:$rootfs_path/$CONTAINERD_REL/$db $rootfs_path/$CONTAINERD_REL/$db && sync -f $rootfs_path/$CONTAINERD_REL/$db && pct start $vmid"
        restore_cmd="$(printf 'Peer clean copy on %s\nsha256: %s\nmtime: %s\nRestore DB only:\n%s\n\nPBS full restore:\n%s' "$peer_node" "$peer_sha" "$peer_mtime" "$peer_restore_cmd" "$restore_cmd")"
    fi

    msg="$(printf \
'⚠️ *containerd DB corrupt* — CT %s on %s
Phase: `%s`
DB: `%s`
bbolt rc: %s
%s

%s

Restore:
`%s`' \
        "$vmid" "$(hostname -s)" "$phase" "$db" "$rc" "$detail" "$auto_restore_note" "$restore_cmd")"

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHATID}" \
        --data-urlencode "text=${msg}" \
        --data-urlencode "parse_mode=Markdown" \
        >/dev/null || true
}

auto_restore_from_peer() {
    local db=$1
    local peer_copy peer_node peer_sha peer_mtime tmp_db

    case "$PHASE" in
        pre-start) ;;
        *)
            return 1
            ;;
    esac

    peer_copy=$(peer_clean_copy "$db" || true)
    [[ -n $peer_copy ]] || return 1

    IFS='|' read -r peer_node peer_sha peer_mtime <<<"$peer_copy"

    tmp_db=$(mktemp /tmp/homelab-docker-bbolt-restore.XXXXXX.db) || return 1
    if ! scp -q -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
        "root@${peer_node}:$db" "$tmp_db"; then
        rm -f "$tmp_db"
        return 1
    fi

    if ! cmp -s "$tmp_db" "$db" 2>/dev/null; then
        if cp --reflink=auto --sparse=always "$tmp_db" "$db" 2>/dev/null; then
            sync -f "$db" 2>/dev/null || sync
            rm -f "$tmp_db"
            printf 'Auto-restored from peer %s (sha256=%s mtime=%s)' "$peer_node" "$peer_sha" "$peer_mtime"
            return 0
        fi
    fi

    rm -f "$tmp_db"
    return 1
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
            auto_restore_note=$(auto_restore_from_peer "$db" || true)
            log "result=FAIL rc=$rc elapsed=${elapsed}s label=$label rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path db=$db size=$size mtime=\"$mtime\" sha256=$sha_before copy_sha256=$sha_copy detail=\"$detail\""
            [[ -n ${auto_restore_note:-} ]] && log "result=RESTORE label=$label db=$db detail=\"$auto_restore_note\""
            notify_corruption "$VMID" "$PHASE" "$label" "$rc" "$detail" "$auto_restore_note"
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
    "$containerd_root"/io.containerd.mount-manager.v1.bolt/meta.db; do
    # io.containerd.snapshotter.v1.overlayfs/metadata.db is excluded: it can
    # be 2GB+ and bbolt check on it takes 60-120s, blocking CT lifecycle hooks.
    # It is also not the database that triggers the containerd panic on corrupt
    # migration snapshots; meta.db (above) is the critical one.
    [[ -f $db ]] || continue
    dbs+=("$db")
done

if [[ ${#dbs[@]} -eq 0 ]]; then
    log "result=SKIP reason=dbs_missing rootfs_ref=$rootfs_ref rootfs_path=$rootfs_path containerd_root=$containerd_root"
    finish
fi

if [[ $PHASE == post-stop ]]; then
    # Filesystem sync is intentionally not performed here.
    # PVE/Replication.pm now calls syncfs() + zpool sync on every ZFS volume
    # mountpoint before taking the migration snapshot, which flushes both the
    # kernel page cache and ZFS dirty TXGs without requiring any hookscript.
    log "sync=skipped reason=handled_by_replication_pm phase=$PHASE db_count=${#dbs[@]}"
fi

for db in "${dbs[@]}"; do
    label=${db#"$containerd_root"/}
    check_db "$db" "$label"
done

finish
