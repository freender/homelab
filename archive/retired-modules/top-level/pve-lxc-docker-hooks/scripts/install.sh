#!/usr/bin/env bash
set -euo pipefail

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

require_file "$BUILD_DIR/env" "$BUILD_DIR/env" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/env"
load_file_map "$BUILD_DIR/file-map.conf"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    print_error "pve-lxc-docker-hooks must run as root"
    exit 1
fi

SNIPPET_DIR=/var/lib/vz/snippets
HOOK_NAME=homelab-docker-bbolt-sync-hook.sh

mkdir -p "$SNIPPET_DIR"
rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

HOMELAB_CONF_DIR=/etc/homelab

IFS=' ' read -r -a vmids <<<"${DOCKER_LXC_VMIDS:-}"
for vmid in "${vmids[@]}"; do
    [[ -n $vmid ]] || continue
    if ! pct config "$vmid" >/dev/null 2>&1; then
        print_warn "CT $vmid not found in cluster config; skipping hookscript"
        continue
    fi
    current=$(pct config "$vmid" 2>/dev/null | awk -F': ' '/^hookscript:/{print $2; exit}' || true)
    desired="local:snippets/$HOOK_NAME"
    if [[ $current == "$desired" && $FORCE_UPDATE != "true" ]]; then
        print_ok "CT $vmid hookscript already set"
        continue
    fi
    pct set "$vmid" --hookscript "$desired"
    print_ok "CT $vmid hookscript set to $desired"
done

legacy_hook=/var/lib/vz/snippets/ct107-bbolt-hook.sh
if [[ -e $legacy_hook ]]; then
    rm -f "$legacy_hook"
    print_ok "legacy ct107-bbolt-hook.sh removed"
fi

legacy_runtime_repair=/usr/local/sbin/homelab-docker-runtime-repair.sh
if [[ -e $legacy_runtime_repair ]]; then
    rm -f "$legacy_runtime_repair"
    print_ok "legacy homelab-docker-runtime-repair.sh removed"
fi

# --- Periodic monitor (every 5 min while CTs are running) ---
SERVICE_NAME=homelab-docker-bbolt-monitor
VMIDS_FILE=/etc/homelab/docker-lxc-vmids

legacy_repair=/usr/local/sbin/homelab-docker-bbolt-repair.sh
if [[ -e $legacy_repair ]]; then
    rm -f "$legacy_repair"
    print_ok "legacy homelab-docker-bbolt-repair.sh removed"
fi

# Write VMID list for the monitor script
mkdir -p "$HOMELAB_CONF_DIR"
printf '%s\n' "${vmids[@]}" > "$VMIDS_FILE"
chmod 640 "$VMIDS_FILE"
print_ok "vmids file written: $VMIDS_FILE"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.timer"
print_ok "${SERVICE_NAME}.timer enabled and started"
