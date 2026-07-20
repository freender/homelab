#!/usr/bin/env bash
# Periodic containerd bbolt DB health monitor.
# Invoked by a systemd timer every 5 minutes on the PVE host.
# Iterates over all running Docker-LXC containers listed in VMIDS_FILE
# and calls the hook script with phase=monitor for each.
set -uo pipefail

HOOK=/var/lib/vz/snippets/homelab-docker-bbolt-sync-hook.sh
VMIDS_FILE=/etc/homelab/docker-lxc-vmids
LOG=/var/log/homelab-docker-bbolt-sync-hook.log

log_monitor() {
    local msg=$*
    printf '%s monitor node=%s %s\n' \
        "$(date -Is)" \
        "$(hostname)" \
        "$msg" | tee -a "$LOG" | systemd-cat -t homelab-docker-bbolt-monitor -p info
}

if [[ ! -x $HOOK ]]; then
    log_monitor "result=SKIP reason=hook_not_found hook=$HOOK"
    exit 0
fi

if [[ ! -f $VMIDS_FILE ]]; then
    log_monitor "result=SKIP reason=vmids_file_missing path=$VMIDS_FILE"
    exit 0
fi

# Read VMIDs — one per line or space-separated
mapfile -t vmids < <(tr ' \t' '\n\n' <"$VMIDS_FILE" | grep -E '^[0-9]+$' || true)

if [[ ${#vmids[@]} -eq 0 ]]; then
    log_monitor "result=SKIP reason=no_vmids_configured"
    exit 0
fi

log_monitor "action=start vmid_count=${#vmids[@]} vmids=\"${vmids[*]}\""

for vmid in "${vmids[@]}"; do
    # Only check containers that are currently running.
    status=$(pct status "$vmid" 2>/dev/null | awk '{print $2}' || true)
    if [[ $status != "running" ]]; then
        log_monitor "vmid=$vmid result=SKIP reason=not_running status=${status:-unknown}"
        continue
    fi

    log_monitor "vmid=$vmid action=check phase=monitor"
    "$HOOK" "$vmid" monitor || true
done

log_monitor "action=done"
