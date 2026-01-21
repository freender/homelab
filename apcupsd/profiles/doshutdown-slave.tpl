#!/bin/bash
# ${HOST} doshutdown - Slave node
# Backup VM shutdown if master action fails

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "Slave shutdown triggered on ${HOST}"

# Backup: shutdown local VMs
for VMID in $(qm list 2>/dev/null | awk '$3=="running"{print $1}'); do
  $LOGGER "Backup shutdown: Stopping VM $VMID on ${HOST}"
  qm shutdown $VMID --timeout 120
done

$LOGGER "Waiting for shutdown command from master"

# Log that we're exiting without triggering host shutdown
$LOGGER "Exiting with code 99 to prevent apccontrol default shutdown (master will shutdown host)"

# Prevent apccontrol default shutdown handling
# Host shutdown will come from master via SSH
exit 99
