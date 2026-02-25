#!/bin/bash

set -euo pipefail

NODE="$(hostname -s)"

log() {
    echo "[pve-ceph-reconcile] $*"
}

if ! command -v ceph >/dev/null 2>&1 || ! command -v pveceph >/dev/null 2>&1 || ! command -v ceph-volume >/dev/null 2>&1; then
    log "ceph CLI tools are missing, skipping"
    exit 0
fi

log "installing Ceph packages (no-subscription repo)"
pveceph install --repository no-subscription --version squid || log "pveceph install failed"

if [[ ! -f /etc/pve/ceph.conf ]]; then
    log "ceph config not present yet (join cluster first), skipping"
    exit 0
fi

if ! timeout 10 ceph -s >/dev/null 2>&1; then
    log "cannot reach ceph cluster right now, skipping"
    exit 0
fi

if ! systemctl is-active --quiet "ceph-mon@${NODE}.service"; then
    log "creating monitor ${NODE}"
    pveceph mon create --monid "$NODE" || log "monitor creation failed or already exists"
fi

if ! systemctl is-active --quiet "ceph-mgr@${NODE}.service"; then
    log "creating manager ${NODE}"
    pveceph mgr create --id "$NODE" || log "manager creation failed or already exists"
fi

if ! systemctl is-active --quiet "ceph-mds@${NODE}.service"; then
    log "creating metadata server ${NODE}"
    pveceph mds create --name "$NODE" || log "metadata server creation failed or already exists"
fi

mapfile -t OSD_IDS < <(
    ceph-volume lvm list --format json 2>/dev/null | python3 -c '
import json
import sys

try:
    raw = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)

ids = set()

if isinstance(raw, dict):
    for key, value in raw.items():
        if str(key).isdigit():
            ids.add(str(key))
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    osd_id = item.get("osd id") or item.get("osd_id")
                    if osd_id is not None and str(osd_id).isdigit():
                        ids.add(str(osd_id))

for osd_id in sorted(ids, key=int):
    print(osd_id)
'
)

if [[ ${#OSD_IDS[@]} -eq 0 ]]; then
    log "no local ceph-volume OSDs found, skipping OSD activation"
    exit 0
fi

log "activating local OSDs: ${OSD_IDS[*]}"
ceph-volume lvm activate --all || log "ceph-volume activation failed"

for osd_id in "${OSD_IDS[@]}"; do
    log "marking osd.${osd_id} in"
    ceph osd in "osd.${osd_id}" || log "failed to mark osd.${osd_id} in"
done
