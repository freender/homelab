#!/usr/bin/env bash
# Manual recovery for corrupt containerd bbolt metadata inside Docker LXCs.
# Run on the PVE node that currently hosts the CT.
set -euo pipefail

BBOLT=${BBOLT:-/usr/local/bin/bbolt}
CONTAINERD_REL=var/lib/containerd
DEFAULT_REDEPLOY_DIR=/mnt/cache/appdata

usage() {
    cat <<'EOF'
Usage: homelab-docker-bbolt-repair.sh <vmid> --yes [--redeploy] [--redeploy-dir PATH]

Manually repairs a Docker LXC whose containerd bbolt DB is corrupt by:
  1. Verifying the CT is running on this PVE node.
  2. Verifying one or more containerd bbolt DB checks fail.
  3. Stopping Docker/containerd inside the CT.
  4. Re-checking stable DB copies after services are stopped.
  5. Backing up and moving only still-corrupt DBs aside.
  6. Starting containerd/docker with fresh DBs for the moved paths.
  7. Optionally running rm.sh and start.sh from the redeploy directory.

Options:
  --yes                Required. Confirms destructive Docker runtime metadata cleanup.
  --redeploy           After Docker starts, run rm.sh and start.sh when present.
  --redeploy-dir PATH  Directory containing rm.sh/start.sh. Default: /mnt/cache/appdata.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '>>> %s\n' "$*"
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

check_db_copy() {
    local db=$1
    local tmp out rc

    CHECK_DETAIL=
    tmp=$(mktemp /tmp/homelab-bbolt-repair.XXXXXX.db)
    out=$(mktemp /tmp/homelab-bbolt-repair.XXXXXX.out)
    if ! cp --reflink=auto --sparse=always "$db" "$tmp" 2>"$out"; then
        CHECK_DETAIL=$(tr '\n' ' ' <"$out" | cut -c1-700)
        rm -f "$tmp" "$out"
        return 125
    fi

    set +e
    timeout 120 "$BBOLT" check "$tmp" >"$out" 2>&1
    rc=$?
    set -e

    CHECK_DETAIL=$(tr '\n' ' ' <"$out" | cut -c1-700)
    rm -f "$tmp" "$out"
    return "$rc"
}

backup_and_move_db() {
    local db=$1
    local rel backup_copy moved_live

    rel=${db#"$containerd_root"/}
    backup_copy="$backup_dir/$rel.corrupt-copy"
    moved_live="$backup_dir/$rel.moved-from-live"

    mkdir -p "$(dirname "$backup_copy")" "$(dirname "$moved_live")"
    cp -a "$db" "$backup_copy"
    sha256sum "$backup_copy" | tee "$backup_copy.sha256"
    mv "$db" "$moved_live"
    install -d -m 0711 "$(dirname "$db")"
}

vmid=${1:-}
[[ -n $vmid ]] || { usage; exit 1; }
shift || true

confirm=false
redeploy=false
redeploy_dir=$DEFAULT_REDEPLOY_DIR
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)
            confirm=true
            ;;
        --redeploy)
            redeploy=true
            ;;
        --redeploy-dir)
            shift
            [[ -n ${1:-} ]] || die "--redeploy-dir requires a path"
            redeploy_dir=$1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
    shift
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run as root on the PVE node"
[[ $vmid =~ ^[0-9]+$ ]] || die "vmid must be numeric"
[[ $confirm == true ]] || die "--yes is required"
[[ -x $BBOLT ]] || die "bbolt not found or not executable: $BBOLT"

status=$(pct status "$vmid" 2>/dev/null | awk '{print $2}' || true)
[[ $status == running ]] || die "CT $vmid is not running on this node (status: ${status:-unknown})"

rootfs_ref=$(pct config "$vmid" 2>/dev/null | awk -F': ' '/^rootfs:/{print $2; exit}' | cut -d, -f1)
[[ -n ${rootfs_ref:-} ]] || die "CT $vmid rootfs reference not found"

rootfs_path=$(pvesm path "$rootfs_ref" 2>/dev/null || true)
if [[ -z ${rootfs_path:-} || ! -d $rootfs_path ]]; then
    volume=${rootfs_ref#*:}
    pool=${rootfs_ref%%:*}
    rootfs_path="/${pool}/${volume}"
fi
[[ -d $rootfs_path ]] || die "CT $vmid rootfs path not found: $rootfs_path"

containerd_root="$rootfs_path/$CONTAINERD_REL"
[[ -d $containerd_root ]] || die "containerd root not found: $containerd_root"

collect_bbolt_dbs
[[ ${#dbs[@]} -gt 0 ]] || die "no containerd bbolt DBs found under $containerd_root"

info "checking ${#dbs[@]} containerd bbolt DB copies for CT $vmid"
failed_dbs=()
for db in "${dbs[@]}"; do
    rel=${db#"$containerd_root"/}
    if check_db_copy "$db"; then
        info "ok: $rel"
    else
        rc=$?
        info "failed before stop: $rel rc=$rc detail=$CHECK_DETAIL"
        failed_dbs+=("$db")
    fi
done

if [[ ${#failed_dbs[@]} -eq 0 ]]; then
    info "all bbolt checks passed; refusing to repair healthy DBs"
    exit 0
fi

ts=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$rootfs_path/root/containerd-bbolt-corrupt-$ts"
info "backup dir inside CT rootfs: $backup_dir"
mkdir -p "$backup_dir"
pct config "$vmid" >"$backup_dir/pct-config.txt" 2>&1 || true

info "stopping Docker and containerd inside CT $vmid"
pct exec "$vmid" -- bash -lc 'systemctl stop docker.service docker.socket containerd.service || true'
sleep 3
pct exec "$vmid" -- bash -lc 'pkill -TERM -x docker-proxy || true; pkill -TERM -x containerd-shim || true'
sleep 3
pct exec "$vmid" -- bash -lc 'pkill -KILL -x docker-proxy || true; pkill -KILL -x containerd-shim || true'

info "re-checking stable bbolt DB copies after Docker/containerd stop"
still_failed_dbs=()
for db in "${dbs[@]}"; do
    [[ -f $db ]] || continue
    rel=${db#"$containerd_root"/}
    if check_db_copy "$db"; then
        info "ok after stop: $rel"
    else
        rc=$?
        info "still failed: $rel rc=$rc detail=$CHECK_DETAIL"
        still_failed_dbs+=("$db")
    fi
done

if [[ ${#still_failed_dbs[@]} -eq 0 ]]; then
    info "all bbolt checks passed after stopping services; leaving DBs in place"
    repaired=false
else
    info "moving ${#still_failed_dbs[@]} corrupt bbolt DB(s) out of live containerd paths"
    for db in "${still_failed_dbs[@]}"; do
        backup_and_move_db "$db"
    done
    repaired=true
fi

info "starting containerd and Docker inside CT $vmid"
pct exec "$vmid" -- bash -lc 'systemctl reset-failed docker.service containerd.service || true; systemctl start containerd; systemctl start docker'
sleep 5
pct exec "$vmid" -- bash -lc 'systemctl is-active containerd docker; docker version >/dev/null'

if [[ $redeploy == true && $repaired == true ]]; then
    info "running redeploy scripts inside CT $vmid when present: $redeploy_dir"
    pct exec "$vmid" -- bash -lc '
        redeploy_dir=$1
        cd "$redeploy_dir"
        [[ -x ./rm.sh && -x ./start.sh ]] || { printf "redeploy scripts missing or not executable in %s\n" "$redeploy_dir" >&2; exit 1; }
        printf "yes\n" | ./rm.sh
        ./start.sh
    ' bash "$redeploy_dir"
elif [[ $redeploy == true ]]; then
    info "skipping redeploy because no DB was moved"
fi

info "container state after repair"
pct exec "$vmid" -- bash -lc 'docker ps --format "table {{.Names}}\t{{.Status}}"; docker ps -a --filter status=exited --format "exited: {{.Names}} {{.Status}}"'
info "done"
