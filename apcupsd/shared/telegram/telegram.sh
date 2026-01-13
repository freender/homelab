#!/bin/bash
set -euo pipefail

# Optional env file for secrets
ENV_FILE="/etc/apcupsd/telegram/telegram.env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

# Read parameters from input flags
while getopts s:d: flag
 do
  case "${flag}" in
    s) TITLE=${OPTARG};;
    d) MESSAGE=${OPTARG};;
  esac
 done

if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_CHATID:-}" ]]; then
  logger -t apcupsd-telegram "Missing TELEGRAM_TOKEN or TELEGRAM_CHATID"
  exit 1
fi

MESSAGE=$(echo -e "$(hostname): ${TITLE:-UPS Alert}\n${MESSAGE:-}")

curl -G -s -k "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHATID}" \
  --data-urlencode "text=${MESSAGE}" ;
