#!/bin/bash
# {{ HOST }} doshutdown - Slave node
# Backup VM/container shutdown if master action fails

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "Slave shutdown triggered on {{ HOST }}"

{% include "_guest-functions.tpl" %}

# Backup: shutdown local VMs and containers
shutdown_running_guests "{{ HOST }}"

$LOGGER "Waiting for shutdown command from master"

# Log that we're exiting without triggering host shutdown
$LOGGER "Exiting with code 99 to prevent apccontrol default shutdown (master will shutdown host)"

# Prevent apccontrol default shutdown handling
# Host shutdown will come from master via SSH
exit 99
