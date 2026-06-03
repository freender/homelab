#!/bin/bash
set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
STATE_DIR="/run/homelab-pve-backup"
STATE_FILE="$STATE_DIR/backup-state.env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

mkdir -p "$STATE_DIR"
rm -f "$STATE_FILE"

pbs_storage_created="false"

if [[ -f "$BUILD_DIR/storage-plan.conf" ]]; then
    print_sub "Configuring standalone PBS storage definitions..."
    bash "$SCRIPT_DIR/scripts/install-pbs-storage.sh" "$HOST" || exit 1

    if [[ -f "$STATE_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$STATE_FILE"
        pbs_storage_created="${PBS_STORAGE_CREATED:-false}"
    fi
else
    print_sub "Standalone PBS storage definitions not configured; skipping"
fi

if [[ "$pbs_storage_created" == "true" && -f "$BUILD_DIR/restore-plan.conf" ]]; then
    print_sub "Fresh standalone install detected; restoring config from PBS..."
    bash "$SCRIPT_DIR/scripts/install-pbs-config-restore.sh" "$HOST" || exit 1
else
    print_sub "PBS config restore not required; skipping"
fi

if [[ -f "$BUILD_DIR/restore-ct-plan.conf" ]]; then
    print_sub "Restoring prepared LXC backups if needed..."
    bash "$SCRIPT_DIR/scripts/install-prepared-lxcs.sh" "$HOST" || exit 1
else
    print_sub "Prepared LXC restore not configured; skipping"
fi

if [[ -f "$BUILD_DIR/jobs-plan.conf" ]]; then
    print_sub "Configuring standalone backup jobs..."
    bash "$SCRIPT_DIR/scripts/install-backup-jobs.sh" "$HOST" || exit 1
else
    print_sub "Standalone backup jobs not configured; skipping"
fi
