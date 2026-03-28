#!/bin/bash
set -e

REPOSITORY="${REPOSITORY}"
BACKUP_ID="${BACKUP_ID}"
ARCHIVE_NAME="${ARCHIVE_NAME}"
CEPH_ENABLED="${CEPH_ENABLED}"

if [[ -z "$REPOSITORY" || -z "$BACKUP_ID" || -z "$ARCHIVE_NAME" ]]; then
    echo "Missing required backup configuration" >&2
    exit 1
fi

if [[ -z "${PBS_PASSWORD:-}" ]]; then
    echo "PBS_PASSWORD is not set" >&2
    exit 1
fi

if ! command -v proxmox-backup-client >/dev/null 2>&1; then
    echo "proxmox-backup-client not found" >&2
    exit 1
fi

declare -a archive_specs
archive_specs=("${ARCHIVE_NAME}.pxar:/etc/pve")

if [[ "$CEPH_ENABLED" == "true" ]]; then
    if [[ -d /etc/ceph ]]; then
        archive_specs+=("etc-ceph.pxar:/etc/ceph")
    else
        logger -t pve-config-backup "Ceph enabled but /etc/ceph missing; skipping etc-ceph archive"
    fi
fi

proxmox-backup-client backup "${archive_specs[@]}" \
    --repository "$REPOSITORY" \
    --backup-type host \
    --backup-id "$BACKUP_ID"

logger -t pve-config-backup "PVE config backup completed: ${BACKUP_ID} -> ${REPOSITORY}"
