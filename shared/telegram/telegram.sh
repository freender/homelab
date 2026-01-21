#!/bin/bash
# shared/telegram/telegram.sh - Universal Telegram notification script
# Used by: apcupsd, zfs/zed
#
# Usage: telegram.sh -s "Subject" -d "Message body"
#
# Env file locations (checked in order):
#   /etc/homelab/telegram.env     (preferred - shared location)
#   /etc/apcupsd/telegram/telegram.env
#   /etc/zfs/zed.d/.env

set -euo pipefail

# Find and source env file
ENV_LOCATIONS=(
    "/etc/homelab/telegram.env"
    "/etc/apcupsd/telegram/telegram.env"
    "/etc/zfs/zed.d/.env"
)

for ENV_FILE in "${ENV_LOCATIONS[@]}"; do
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        break
    fi
done

# Parse arguments
while getopts s:d: flag; do
    case "${flag}" in
        s) TITLE="${OPTARG}" ;;
        d) MESSAGE="${OPTARG}" ;;
        *) ;;
    esac
done

# Validate credentials
if [[ -z "${TELEGRAM_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHATID:-}" ]]; then
    logger -t telegram-notify "Missing TELEGRAM_TOKEN or TELEGRAM_CHATID"
    exit 1
fi

# Format and send message
HOSTNAME=$(hostname)
FULL_MESSAGE=$(echo -e "${HOSTNAME}: ${TITLE:-Alert}\n${MESSAGE:-}")

curl -G -s -k "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHATID}" \
    --data-urlencode "text=${FULL_MESSAGE}"
