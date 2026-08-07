#!/bin/bash
# {{ HOST }} doshutdown - Independent master
# Power off normally; Proxmox handles local guest shutdown. No cluster dependencies.

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "{{ HOST }} UPS battery critical - initiating shutdown"

# Power off {{ HOST }}. Proxmox handles the graceful shutdown of local guests.
$LOGGER "Scheduling {{ HOST }} poweroff in 2 seconds"
nohup sh -c 'sleep 2 && logger -t apcupsd-shutdown "Executing poweroff on {{ HOST }}" && systemctl poweroff' >/dev/null 2>&1 &

# Exit immediately with code 99 to prevent apccontrol from running its default shutdown
$LOGGER "Exiting doshutdown with code 99 ({{ HOST }} poweroff scheduled in background)"
exit 99
