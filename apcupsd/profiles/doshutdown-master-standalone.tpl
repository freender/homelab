#!/bin/bash
# ${HOST} doshutdown - Independent master
# Simple self-shutdown, no cluster dependencies

LOGGER="logger -t apcupsd-shutdown"
$LOGGER "${HOST} UPS battery critical - shutting down"

# Notify via Telegram
/etc/apcupsd/telegram/telegram.sh -s "SHUTDOWN" -d "${HOST} UPS critical - shutting down"

# Schedule poweroff in background with 2 second delay
$LOGGER "Scheduling ${HOST} poweroff in 2 seconds"
nohup sh -c 'sleep 2 && logger -t apcupsd-shutdown "Executing poweroff on ${HOST}" && systemctl poweroff' >/dev/null 2>&1 &

# Exit immediately with code 99 to prevent apccontrol from running its default shutdown
$LOGGER "Exiting doshutdown with code 99 (${HOST} poweroff scheduled in background)"
exit 99
