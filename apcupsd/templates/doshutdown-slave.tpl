#!/bin/bash
# {{ HOST }} doshutdown - Slave node
# Disarm HA then let Proxmox perform its native local shutdown.

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "Slave shutdown triggered on {{ HOST }}"

{% include "_ha-functions.tpl" %}

# The master normally gets here first, but each slave must be safe if it receives
# the UPS event before the master's cluster-wide shutdown command.
if ! disarm_ha; then
  $LOGGER "ERROR: Failed to disarm HA; refusing local guest shutdown to avoid migration"
  exit 1
fi

$LOGGER "Scheduling local poweroff; Proxmox will stop local guests"
nohup sh -c 'sleep 2 && logger -t apcupsd-shutdown "Executing poweroff on {{ HOST }}" && systemctl poweroff' >/dev/null 2>&1 &

# Log that we're exiting without triggering apccontrol's duplicate shutdown path.
$LOGGER "Exiting with code 99 ({{ HOST }} poweroff scheduled)"

# Prevent apccontrol default shutdown handling.
exit 99
