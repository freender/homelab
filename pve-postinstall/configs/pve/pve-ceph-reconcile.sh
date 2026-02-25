#!/bin/bash

set -euo pipefail

NODE="$(hostname -s)"

log() {
    echo "[pve-ceph-reconcile] $*"
}

if ! command -v ceph >/dev/null 2>&1 || ! command -v pveceph >/dev/null 2>&1; then
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
