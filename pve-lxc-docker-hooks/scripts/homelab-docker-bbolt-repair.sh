#!/usr/bin/env bash
# Manual recovery for corrupt containerd bbolt metadata inside Docker LXCs.
# Run on the PVE node that currently hosts the CT.
set -euo pipefail

BBOLT=${BBOLT:-/usr/local/bin/bbolt}
CONTAINERD_REL=var/lib/containerd
APPDATA_ROOT=/mnt/cache/appdata

usage() {
    cat <<'EOF'
Usage: homelab-docker-bbolt-repair.sh <vmid> --yes [--redeploy]

Manually repairs a Docker LXC whose containerd metadata DB is corrupt by:
  1. Verifying the CT is running on this PVE node.
  2. Verifying containerd metadata bbolt check fails.
  3. Stopping Docker/containerd inside the CT.
  4. Backing up and moving the corrupt metadata DB aside.
  5. Starting containerd/docker with a fresh metadata DB.
  6. Optionally running /mnt/cache/appdata/rm.sh and start.sh.

Options:
  --yes       Required. Confirms destructive Docker runtime metadata cleanup.
  --redeploy  After Docker starts, run appdata rm.sh and start.sh.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '>>> %s\n' "$*"
}

vmid=${1:-}
[[ -n $vmid ]] || { usage; exit 1; }
shift || true

confirm=false
redeploy=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)
            confirm=true
            ;;
        --redeploy)
            redeploy=true
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

db="$rootfs_path/$CONTAINERD_REL/io.containerd.metadata.v1.bolt/meta.db"
[[ -f $db ]] || die "containerd metadata DB not found: $db"

tmp=$(mktemp /tmp/homelab-bbolt-repair.XXXXXX.db)
out=$(mktemp /tmp/homelab-bbolt-repair.XXXXXX.out)
cleanup() {
    rm -f "$tmp" "$out"
}
trap cleanup EXIT

info "checking containerd DB copy for CT $vmid"
cp --reflink=auto --sparse=always "$db" "$tmp"
if timeout 120 "$BBOLT" check "$tmp" >"$out" 2>&1; then
    info "bbolt check passed; refusing to repair a healthy DB"
    exit 0
fi
rc=$?
detail=$(tr '\n' ' ' <"$out" | cut -c1-700)
info "bbolt check failed rc=$rc detail=$detail"

ts=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$rootfs_path/root/containerd-bbolt-corrupt-$ts"
info "backup dir inside CT rootfs: $backup_dir"
mkdir -p "$backup_dir"
cp -a "$db" "$backup_dir/meta.db.corrupt-copy"
sha256sum "$backup_dir/meta.db.corrupt-copy" | tee "$backup_dir/meta.db.corrupt-copy.sha256"
pct config "$vmid" >"$backup_dir/pct-config.txt" 2>&1 || true

info "stopping Docker and containerd inside CT $vmid"
pct exec "$vmid" -- bash -lc 'systemctl stop docker.service docker.socket containerd.service || true'
sleep 3
pct exec "$vmid" -- bash -lc 'pkill -TERM -x docker-proxy || true; pkill -TERM -x containerd-shim || true'
sleep 3
pct exec "$vmid" -- bash -lc 'pkill -KILL -x docker-proxy || true; pkill -KILL -x containerd-shim || true'

info "moving corrupt DB out of live containerd path"
mv "$db" "$backup_dir/meta.db.moved-from-live"
install -d -m 0711 "$(dirname "$db")"

info "starting containerd and Docker inside CT $vmid"
pct exec "$vmid" -- bash -lc 'systemctl reset-failed docker.service containerd.service || true; systemctl start containerd; systemctl start docker'
sleep 5
pct exec "$vmid" -- bash -lc 'systemctl is-active containerd docker; docker version >/dev/null'

if [[ $redeploy == true ]]; then
    info "running appdata rm.sh and start.sh inside CT $vmid"
    pct exec "$vmid" -- bash -lc "cd '$APPDATA_ROOT' && printf 'yes\n' | ./rm.sh && ./start.sh"
fi

info "container state after repair"
pct exec "$vmid" -- bash -lc 'docker ps --format "table {{.Names}}\t{{.Status}}"; docker ps -a --filter status=exited --format "exited: {{.Names}} {{.Status}}"'
info "done"
