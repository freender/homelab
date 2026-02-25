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

log "activating local OSDs"
ceph-volume lvm activate --all || log "ceph-volume activation failed"

mapfile -t OSD_IDS < <(
    systemctl list-units 'ceph-osd@*.service' --no-pager --no-legend 2>/dev/null \
        | awk -F'[@.]' '/ceph-osd@[0-9]+\.service/ {print $2}' \
        | sort -u
)

if [[ ${#OSD_IDS[@]} -eq 0 ]]; then
    log "no local OSD services found after activation"
else
    log "marking local OSDs in: ${OSD_IDS[*]}"
    for osd_id in "${OSD_IDS[@]}"; do
        ceph osd in "osd.${osd_id}" || log "failed to mark osd.${osd_id} in"
    done
fi

if ceph osd dump 2>/dev/null | grep -qE '^flags .*noout'; then
    log "noout is set; unsetting noout"
    ceph osd unset noout || log "failed to unset noout"
else
    log "noout is not set"
fi
