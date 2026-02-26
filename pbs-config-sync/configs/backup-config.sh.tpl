#!/bin/bash
set -euo pipefail

DEST_USER="${DEST_USER}"
DEST_HOST="${DEST_HOST}"
DEST_PATH="${DEST_PATH}"

if [[ -z "$DEST_USER" || -z "$DEST_HOST" || -z "$DEST_PATH" ]]; then
    echo "Missing destination config" >&2
    exit 1
fi

REMOTE="${DEST_USER}@${DEST_HOST}:${DEST_PATH}"

rsync -a --delete /etc/proxmox-backup/ "$REMOTE/etc/"
rsync -a --delete /var/lib/proxmox-backup/ "$REMOTE/lib/"
rsync -a --delete /root/.ssh/ "$REMOTE/ssh/"

rsync -a --delete /etc/network/interfaces "$REMOTE/host/interfaces"
rsync -a --delete /etc/hostname "$REMOTE/host/hostname"
rsync -a --delete /etc/hosts "$REMOTE/host/hosts"
rsync -a --delete /etc/apt/sources.list.d/ "$REMOTE/host/sources.list.d/"

logger -t backup-config "PBS config sync to ${DEST_USER}@${DEST_HOST}:${DEST_PATH} completed"
