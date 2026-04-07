#!/bin/bash

set -e

TARGET_DIR=${1:-/mnt/cache/backup/appdata}

if [[ ! -d "$TARGET_DIR" ]]; then
    exit 0
fi

find "$TARGET_DIR" \( ! -user 1000 -o ! -group 1000 \) -exec chown 1000:1000 {} +
