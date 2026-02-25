#!/bin/bash

set -euo pipefail

NODE="$(hostname -s)"

log() {
    echo "[pve-ceph-reconcile] $*"
}

osd_ids_from_systemd() {
    systemctl list-units 'ceph-osd@*.service' --no-pager --no-legend 2>/dev/null \
        | awk -F'[@.]' '/ceph-osd@[0-9]+\.service/ {print $2}' \
        | sort -u
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

mapfile -t OLD_OSD_IDS < <(osd_ids_from_systemd)

ceph-volume lvm activate --all >/dev/null 2>&1 || log "ceph-volume activation failed"

mapfile -t NEW_OSD_IDS < <(osd_ids_from_systemd)

declare -A old_osd_lookup=()
for osd_id in "${OLD_OSD_IDS[@]}"; do
    old_osd_lookup["$osd_id"]=1
done

NEW_IMPORT_IDS=()
for osd_id in "${NEW_OSD_IDS[@]}"; do
    if [[ -z "${old_osd_lookup[$osd_id]+x}" ]]; then
        NEW_IMPORT_IDS+=("$osd_id")
    fi
done

if [[ ${#NEW_IMPORT_IDS[@]} -eq 0 ]]; then
    log "OSD diff: no new OSDs"
else
    log "OSD diff: +${NEW_IMPORT_IDS[*]}"
    for osd_id in "${NEW_IMPORT_IDS[@]}"; do
        ceph osd in "osd.${osd_id}" || log "failed to mark osd.${osd_id} in"
    done
fi

ceph osd unset noout >/dev/null 2>&1 || true
