#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RULES_SOURCE_DIR="$SCRIPT_DIR/rules"
RULES_DEST_DIR="/mnt/cache/appdata/vmalert/rules"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$RULES_SOURCE_DIR/critical-containers.yml" "critical vmalert rules" || exit 1
require_file "$RULES_SOURCE_DIR/docker.yml" "docker vmalert rules" || exit 1
require_file "$RULES_SOURCE_DIR/important-containers.yml" "important vmalert rules" || exit 1
require_file "$RULES_SOURCE_DIR/systemd-failed.yml" "systemd vmalert rules" || exit 1
require_file "$RULES_SOURCE_DIR/ups.yml" "UPS vmalert rules" || exit 1
require_file "$RULES_SOURCE_DIR/zfs-pools.yml" "ZFS vmalert rules" || exit 1

if [[ ! -d "$RULES_DEST_DIR" ]]; then
    print_error "vmalert rules directory is missing: $RULES_DEST_DIR"
    exit 1
fi

for destination in "$RULES_DEST_DIR"/*.yml; do
    [[ -e "$destination" ]] || continue
    if [[ ! -f "$RULES_SOURCE_DIR/${destination##*/}" ]]; then
        print_error "unmanaged active vmalert rule: $destination"
        exit 1
    fi
done

print_sub "Validating staged vmalert rules..."
docker run --rm -v "$RULES_SOURCE_DIR:/rules:ro" victoriametrics/vmalert:v1.117.1 \
    -rule='/rules/*.yml' -dryRun

rules_changed=false
for source in "$RULES_SOURCE_DIR"/*.yml; do
    destination="$RULES_DEST_DIR/${source##*/}"
    rc=0
    copy_if_changed "$source" "$destination" "vmalert rule ${source##*/}" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && rules_changed=true
done

if [[ "$rules_changed" == "true" ]]; then
    print_sub "Restarting vmalert to load updated rules..."
    docker restart vmalert >/dev/null
    print_ok "vmalert restarted"
else
    print_ok "vmalert rules unchanged"
fi
