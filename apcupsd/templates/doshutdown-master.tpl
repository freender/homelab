#!/bin/bash
# {{ HOST }} doshutdown - Master cluster controller
# Disarm HA, then let each Proxmox node perform its native shutdown.

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "UPS battery critical - initiating cluster shutdown"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"

SLAVE_HOSTS=({{ SLAVE_HOSTS }})

{% include "_ha-functions.tpl" %}

# PHASE 0: Prevent HA from restarting or relocating intentionally stopped guests.
# The desired state of every HA resource remains "started" for automatic recovery.
$LOGGER "PHASE 0: Disarming HA while preserving resource desired states"
if ! disarm_ha; then
  $LOGGER "ERROR: Failed to disarm HA; refusing cluster shutdown to avoid guest migration"
  exit 1
fi

# PHASE 1: Power off nodes. Proxmox handles local guest shutdown as part of its
# normal systemd shutdown transaction; HA is already disarmed above.
$LOGGER "PHASE 1: Powering off nodes: slaves immediate, {{ HOST }} in 30 seconds"

# Poweroff slaves immediately
for NODE in "${SLAVE_HOSTS[@]}"; do
  ssh $SSH_OPTS "$NODE" "nohup sh -c 'sleep 2 && logger -t apcupsd-shutdown \"Executing poweroff on $NODE\" && systemctl poweroff' >/dev/null 2>&1 &"
done

# Schedule master poweroff in background (30 seconds delay) so script can exit immediately with code 99
$LOGGER "Scheduling {{ HOST }} poweroff in 30 seconds"
nohup sh -c 'sleep 30 && logger -t apcupsd-shutdown "Executing poweroff on {{ HOST }}" && systemctl poweroff' >/dev/null 2>&1 &

# Exit immediately with code 99 to prevent apccontrol from running its default shutdown
$LOGGER "Exiting doshutdown with code 99 ({{ HOST }} poweroff scheduled in background)"
exit 99
