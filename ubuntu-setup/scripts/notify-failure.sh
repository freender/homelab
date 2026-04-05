#!/bin/bash
# notify-failure.sh - Send Telegram alert when a systemd unit fails.
#
# Invoked by: homelab-notify-failure@.service
# Receives:   FAILED_UNIT as argument $1 (the %i instance from the template unit)
#
# Env file search order:
#   /etc/homelab/telegram.env   (preferred shared location)
#   /etc/apcupsd/telegram/telegram.env (fallback - reuses apcupsd creds)

set -euo pipefail

FAILED_UNIT="${1:-unknown}"

ENV_LOCATIONS=(
    "/etc/homelab/telegram.env"
    "/etc/apcupsd/telegram/telegram.env"
)

for ENV_FILE in "${ENV_LOCATIONS[@]}"; do
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        break
    fi
done

if [[ -z "${TELEGRAM_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHATID:-}" ]]; then
    logger -t homelab-notify "Missing TELEGRAM_TOKEN or TELEGRAM_CHATID; cannot send alert for $FAILED_UNIT"
    exit 0
fi

HOSTNAME=$(hostname -s)

# Grab last 10 journal lines from the failed unit (best-effort)
JOURNAL_TAIL=$(journalctl -u "$FAILED_UNIT" --no-pager -n 10 --output=short-monotonic 2>/dev/null || true)

MESSAGE=$(printf "🔴 *%s* — unit failed\n\`%s\`\n\n%s" \
    "$HOSTNAME" "$FAILED_UNIT" "$JOURNAL_TAIL")

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHATID}" \
    --data-urlencode "text=${MESSAGE}" \
    --data-urlencode "parse_mode=Markdown" \
    >/dev/null
