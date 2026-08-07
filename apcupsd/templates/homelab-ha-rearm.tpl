#!/bin/bash
# Re-arm Proxmox HA after a coordinated UPS shutdown once quorum is available.

set -euo pipefail

LOGGER=(logger -t homelab-ha-rearm)

while ! pvecm status 2>/dev/null | awk '/^Quorate:/ { found=1; exit ($2 == "Yes" ? 0 : 1) } END { if (!found) exit 1 }'; do
    "${LOGGER[@]}" "Waiting for cluster quorum before re-arming HA"
    sleep 15
done

if ha-manager status | grep -q '^fencing armed'; then
    "${LOGGER[@]}" "HA is already armed"
    exit 0
fi

"${LOGGER[@]}" "Cluster quorum established; re-arming HA"
if ha-manager crm-command arm-ha; then
    "${LOGGER[@]}" "HA re-armed; managed resources will recover to their desired states"
elif ha-manager status | grep -q '^fencing armed'; then
    "${LOGGER[@]}" "HA was re-armed by another cluster node"
else
    "${LOGGER[@]}" "Failed to re-arm HA"
    exit 1
fi
