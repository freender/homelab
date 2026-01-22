#!/bin/bash
# ${HOST} doshutdown - Master cluster controller
# Stop ALL VMs cluster-wide, wait for them to stop, then poweroff hosts

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "UPS battery critical - initiating cluster shutdown"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"

# Notify via Telegram
/etc/apcupsd/telegram/telegram.sh -s "SHUTDOWN" -d "${HOST} UPS critical - initiating cluster shutdown"

SLAVE_HOSTS=(${SLAVE_HOSTS})

# PHASE 1: Shutdown ALL VMs on ALL nodes simultaneously
$LOGGER "PHASE 1: Initiating graceful shutdown of all VMs cluster-wide"

for NODE in "${SLAVE_HOSTS[@]}"; do
  ssh $SSH_OPTS "$NODE" "
    for VMID in \$(qm list 2>/dev/null | awk '\$3==\"running\"{print \$1}'); do
      logger -t apcupsd-shutdown \"Shutting down VM \$VMID on ${NODE}\"
      qm shutdown \$VMID --timeout 120 &
    done
  " &
done

# Shutdown VMs on master
for VMID in $(qm list 2>/dev/null | awk '$3=="running"{print $1}'); do
  $LOGGER "Shutting down VM $VMID on ${HOST}"
  qm shutdown $VMID --timeout 120 &
done

# Give VMs a moment to start shutting down
sleep 3

# PHASE 2: Wait for ALL VMs to stop on ALL nodes
$LOGGER "PHASE 2: Waiting for all VMs to stop cluster-wide (max 120 seconds)"

for i in {1..120}; do
  ALL_STOPPED=true

  for NODE in "${SLAVE_HOSTS[@]}"; do
    NODE_RUNNING=$(ssh $SSH_OPTS "$NODE" "qm list 2>/dev/null | awk '\$3==\"running\"{print \$1}'" 2>/dev/null)
    if [ -n "$NODE_RUNNING" ]; then
      ALL_STOPPED=false
    fi
  done

  MASTER_RUNNING=$(qm list 2>/dev/null | awk '$3=="running"{print $1}')
  if [ -n "$MASTER_RUNNING" ]; then
    ALL_STOPPED=false
  fi

  if [ "$ALL_STOPPED" = true ]; then
    $LOGGER "All VMs stopped on all nodes after $i seconds"
    break
  fi

  sleep 1
done

# Log if we hit timeout
if [ "$ALL_STOPPED" = false ]; then
  $LOGGER "WARNING: Timeout waiting for VMs to stop, forcing host shutdown anyway"
  for NODE in "${SLAVE_HOSTS[@]}"; do
    NODE_RUNNING=$(ssh $SSH_OPTS "$NODE" "qm list 2>/dev/null | awk '\$3==\"running\"{print \$1}'" 2>/dev/null)
    [ -n "$NODE_RUNNING" ] && $LOGGER "$NODE still has running VMs: $NODE_RUNNING"
  done
  [ -n "$MASTER_RUNNING" ] && $LOGGER "${HOST} still has running VMs: $MASTER_RUNNING"
fi

# PHASE 3: Poweroff all hosts (slaves immediate, master after 30 seconds)
$LOGGER "PHASE 3: Powering off hosts: slaves immediate, ${HOST} in 30 seconds"
/etc/apcupsd/telegram/telegram.sh -s "SHUTDOWN" -d "All VMs stopped - powering off cluster (slaves now, ${HOST} in 30s)"

# Poweroff slaves immediately
for NODE in "${SLAVE_HOSTS[@]}"; do
  ssh $SSH_OPTS "$NODE" "nohup sh -c 'sleep 2 && logger -t apcupsd-shutdown \"Executing poweroff on $NODE\" && systemctl poweroff' >/dev/null 2>&1 &"
done

# Schedule master poweroff in background (30 seconds delay) so script can exit immediately with code 99
$LOGGER "Scheduling ${HOST} poweroff in 30 seconds"
nohup sh -c 'sleep 30 && logger -t apcupsd-shutdown "Executing poweroff on ${HOST}" && systemctl poweroff' >/dev/null 2>&1 &

# Exit immediately with code 99 to prevent apccontrol from running its default shutdown
$LOGGER "Exiting doshutdown with code 99 (${HOST} poweroff scheduled in background)"
exit 99
