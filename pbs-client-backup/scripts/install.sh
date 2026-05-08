#!/bin/bash
# install.sh - Install PBS client backup script and timer

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1
load_file_map

print_header "PBS Client Backup"

for required in \
    homelab-pbs-client-backup \
    homelab-pbs-client-backup.conf \
    homelab-pbs-client-backup.env \
    homelab-pbs-client-backup.service \
    homelab-pbs-client-backup.timer; do
    require_file "$BUILD_DIR/$required" "$BUILD_DIR/$required" || exit 1
done

if ! command -v zfs >/dev/null 2>&1; then
    print_error "zfs command not found"
    exit 1
fi

if ! command -v docker >/dev/null 2>&1 && ! command -v proxmox-backup-client >/dev/null 2>&1; then
    print_error "neither docker nor proxmox-backup-client found"
    exit 1
fi

changed=false
for file_name in "${!FILE_MAP_DEST[@]}"; do
    rc=0
    install_build_file "$file_name" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && changed=true
done

if [[ "$changed" == true ]]; then
    systemctl daemon-reload
fi

systemctl enable --now homelab-pbs-client-backup.timer >/dev/null
print_ok "homelab-pbs-client-backup.timer enabled"

systemctl list-timers homelab-pbs-client-backup.timer --no-pager --all || true
print_ok "PBS client backup installed"
