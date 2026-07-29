#!/bin/bash
# {{ HOST }} doshutdown - Independent master
# Stop local VMs and containers, verify, then self-poweroff. No cluster dependencies.

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "{{ HOST }} UPS battery critical - initiating shutdown"

{% include "_guest-functions.tpl" %}

# PHASE 1: Shutdown all local VMs and containers
$LOGGER "PHASE 1: Initiating graceful shutdown of all VMs and containers on {{ HOST }}"
shutdown_running_guests "{{ HOST }}"

# Give VMs/containers a moment to start shutting down
sleep 3

# PHASE 2: Wait for all local VMs and containers to stop (max 120 seconds)
$LOGGER "PHASE 2: Waiting for all VMs and containers to stop on {{ HOST }} (max 120 seconds)"

ALL_STOPPED=false
for i in {1..120}; do
  RUNNING=$(list_running_guests)
  if [ -z "$RUNNING" ]; then
    $LOGGER "All VMs and containers stopped on {{ HOST }} after $i seconds"
    ALL_STOPPED=true
    break
  fi
  sleep 1
done

if [ "$ALL_STOPPED" = false ]; then
  $LOGGER "WARNING: Timeout waiting for VMs/containers to stop on {{ HOST }}, forcing poweroff anyway: $RUNNING"
fi

# PHASE 3: Poweroff {{ HOST }}
$LOGGER "PHASE 3: Scheduling {{ HOST }} poweroff in 2 seconds"
nohup sh -c 'sleep 2 && logger -t apcupsd-shutdown "Executing poweroff on {{ HOST }}" && systemctl poweroff' >/dev/null 2>&1 &

# Exit immediately with code 99 to prevent apccontrol from running its default shutdown
$LOGGER "Exiting doshutdown with code 99 ({{ HOST }} poweroff scheduled in background)"
exit 99
