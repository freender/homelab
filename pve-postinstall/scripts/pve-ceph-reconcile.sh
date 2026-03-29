#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE="$(hostname -s)"
CEPH_CONF_FILE="/etc/pve/ceph.conf"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

log() { print_sub "$*"; }

osd_ids_from_systemd() {
    systemctl list-units 'ceph-osd@*.service' --no-pager --no-legend 2>/dev/null \
        | awk -F'[@.]' '/ceph-osd@[0-9]+\.service/ {print $2}' \
        | sort -u
}

ensure_ceph_cli_bootstrap() {
    local public_network=""

    if [[ ! -f "$CEPH_CONF_FILE" ]]; then
        log "ceph config not present yet (join cluster first), skipping"
        return 0
    fi

    public_network="$(awk -F'=' '/^[[:space:]]*public_network[[:space:]]*=/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' "$CEPH_CONF_FILE")"
    if [[ -z "$public_network" ]]; then
        log "public_network missing in $CEPH_CONF_FILE; cannot run pveceph init"
        return 0
    fi

    log "running pveceph init to refresh ceph CLI bootstrap"
    pveceph init --network "$public_network" || log "pveceph init failed"
}

if ! command -v pveceph >/dev/null 2>&1; then
    log "pveceph command is missing, skipping"
    exit 0
fi

if ! command -v ceph >/dev/null 2>&1 || ! command -v ceph-volume >/dev/null 2>&1; then
    log "ceph CLI tools missing; installing Ceph packages"
fi

log "installing Ceph packages (no-subscription repo)"
printf 'y\n' | DEBIAN_FRONTEND=noninteractive pveceph install --repository no-subscription --version squid || log "pveceph install failed"

if ! command -v ceph >/dev/null 2>&1 || ! command -v ceph-volume >/dev/null 2>&1; then
    log "ceph CLI tools still missing after install, skipping"
    exit 0
fi

ensure_ceph_cli_bootstrap

if [[ ! -f "$CEPH_CONF_FILE" ]]; then
    exit 0
fi

export CEPH_CONF="$CEPH_CONF_FILE"

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
