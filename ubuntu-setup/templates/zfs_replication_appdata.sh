#!/bin/bash

set -euo pipefail

SOURCE_DATASET="{{ ZFS_POOL }}/appdata"
TARGET_DATASET="{{ ZFS_POOL }}/backup/appdata"

if ! command -v syncoid >/dev/null 2>&1; then
    echo "syncoid is not installed" >&2
    exit 1
fi

if ! zfs list -H "$SOURCE_DATASET" >/dev/null 2>&1; then
    echo "source dataset not found: $SOURCE_DATASET" >&2
    exit 1
fi

exec /usr/sbin/syncoid -r --delete-target-snapshots --force-delete "$SOURCE_DATASET" "$TARGET_DATASET"
