#!/bin/bash
# ZED Telegram Notifier
# Sends ZFS events to Telegram (errors only)
#
# Install: symlink to /etc/zfs/zed.d/ for each event type
# Requires: /etc/zfs/zed.d/.env with TELEGRAM_TOKEN and TELEGRAM_CHAT_ID

set -euo pipefail

# Load credentials
ENV_FILE="/etc/zfs/zed.d/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    logger -t zed-telegram "ERROR: $ENV_FILE not found"
    exit 1
fi
source "$ENV_FILE"

if [[ -z "${TELEGRAM_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    logger -t zed-telegram "ERROR: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set"
    exit 1
fi

# Send Telegram message
send_telegram() {
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode="HTML" \
        -d text="${message}" > /dev/null 2>&1
}

# Get host info
HOST=$(hostname)
POOL="${ZEVENT_POOL:-unknown}"
SUBCLASS="${ZEVENT_SUBCLASS:-unknown}"
TIME="${ZEVENT_TIME_STRING:-$(date)}"

# Handle events by type
case "${SUBCLASS}" in
    statechange)
        STATE="${ZEVENT_VDEV_STATE_STR:-}"
        # Only notify on error states
        if [[ "$STATE" =~ ^(FAULTED|DEGRADED|REMOVED|UNAVAIL)$ ]]; then
            MSG="<b>ZFS ALERT - ${HOST}</b>

Pool: <code>${POOL}</code>
State: <b>${STATE}</b>
Device: ${ZEVENT_VDEV_PATH:-N/A}
Time: ${TIME}"
            send_telegram "$MSG"
            logger -t zed-telegram "Sent statechange alert: ${POOL} ${STATE}"
        fi
        ;;

    scrub_finish)
        # Check for errors in scrub output
        POOL_STATUS=$(zpool status "${POOL}" 2>/dev/null || echo "")
        ERRORS=$(echo "$POOL_STATUS" | grep -E "errors:" | grep -v "No known data errors" || true)
        SCAN_ERRORS=$(echo "$POOL_STATUS" | grep -oP "with \K\d+(?= errors)" || echo "0")
        
        # Only notify if there were errors
        if [[ -n "$ERRORS" ]] || [[ "$SCAN_ERRORS" -gt 0 ]]; then
            MSG="<b>ZFS Scrub ERRORS - ${HOST}</b>

Pool: <code>${POOL}</code>
Errors: ${SCAN_ERRORS}
Time: ${TIME}"
            send_telegram "$MSG"
            logger -t zed-telegram "Sent scrub_finish alert: ${POOL} ${SCAN_ERRORS} errors"
        else
            logger -t zed-telegram "Scrub completed OK: ${POOL}"
        fi
        ;;

    resilver_finish)
        # Always notify on resilver completion (it means a drive was replaced/rebuilt)
        MSG="<b>ZFS Resilver Complete - ${HOST}</b>

Pool: <code>${POOL}</code>
Time: ${TIME}"
        send_telegram "$MSG"
        logger -t zed-telegram "Sent resilver_finish alert: ${POOL}"
        ;;

    data)
        # Data corruption detected
        MSG="<b>ZFS Data Error - ${HOST}</b>

Pool: <code>${POOL}</code>
Time: ${TIME}

Check: <code>zpool status ${POOL}</code>"
        send_telegram "$MSG"
        logger -t zed-telegram "Sent data error alert: ${POOL}"
        ;;

    io_failure)
        MSG="<b>ZFS I/O Failure - ${HOST}</b>

Pool: <code>${POOL}</code>
Device: ${ZEVENT_VDEV_PATH:-N/A}
Time: ${TIME}"
        send_telegram "$MSG"
        logger -t zed-telegram "Sent io_failure alert: ${POOL}"
        ;;

    checksum)
        MSG="<b>ZFS Checksum Error - ${HOST}</b>

Pool: <code>${POOL}</code>
Device: ${ZEVENT_VDEV_PATH:-N/A}
Time: ${TIME}"
        send_telegram "$MSG"
        logger -t zed-telegram "Sent checksum alert: ${POOL}"
        ;;

    *)
        # Unknown event, log but do not notify
        logger -t zed-telegram "Unhandled event: ${SUBCLASS} for pool ${POOL}"
        ;;
esac

exit 0
