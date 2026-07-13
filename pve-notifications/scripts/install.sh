#!/bin/bash
set -euo pipefail

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/notification-plan.conf"
TELEGRAM_ENV="$BUILD_DIR/telegram.env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

cleanup_secret() {
    if [[ -f "$TELEGRAM_ENV" ]]; then
        if command -v shred >/dev/null 2>&1; then
            shred -u -n 1 "$TELEGRAM_ENV"
        else
            rm -f "$TELEGRAM_ENV"
        fi
    fi
}
trap cleanup_secret EXIT

require_file "$PLAN_FILE" "$PLAN_FILE" || exit 1
require_file "$TELEGRAM_ENV" "$TELEGRAM_ENV" || exit 1

# shellcheck disable=SC1090
source "$PLAN_FILE"
# shellcheck disable=SC1090
source "$TELEGRAM_ENV"

if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_CHATID:-}" ]]; then
    print_error "TELEGRAM_TOKEN or TELEGRAM_CHATID missing"
    exit 1
fi

if ! command -v pvesh >/dev/null 2>&1; then
    print_error "pvesh command not found"
    exit 1
fi

b64() {
    printf '%s' "$1" | base64 | tr -d '\n'
}

body_template='{"chat_id":"{{ secrets.chat_id }}","text":"{{ escape title }}\n\n{{ escape message }}","parse_mode":"Markdown"}'
url_template='https://api.telegram.org/bot{{ secrets.token }}/sendMessage'
header_prop="name=Content-Type,value=$(b64 'application/json')"
token_secret="name=token,value=$(b64 "$TELEGRAM_TOKEN")"
chat_secret="name=chat_id,value=$(b64 "$TELEGRAM_CHATID")"
body_b64="$(b64 "$body_template")"

print_sub "Configuring Telegram webhook target $TARGET_NAME..."
if pvesh get "/cluster/notifications/endpoints/webhook/$TARGET_NAME" >/dev/null 2>&1; then
    pvesh set "/cluster/notifications/endpoints/webhook/$TARGET_NAME" \
        --method post \
        --url "$url_template" \
        --header "$header_prop" \
        --secret "$token_secret" \
        --secret "$chat_secret" \
        --body "$body_b64"
else
    pvesh create /cluster/notifications/endpoints/webhook \
        --name "$TARGET_NAME" \
        --method post \
        --url "$url_template" \
        --header "$header_prop" \
        --secret "$token_secret" \
        --secret "$chat_secret" \
        --body "$body_b64"
fi

for (( i=0; i<${REMOVE_MATCHER_COUNT:-0}; i++ )); do
    matcher_var="REMOVE_MATCHER_${i}"
    matcher="${!matcher_var}"
    if [[ "$matcher" != "$MATCHER_NAME" ]]; then
        pvesh delete "/cluster/notifications/matchers/$matcher" >/dev/null 2>&1 || true
    fi
done

for (( i=0; i<${REMOVE_WEBHOOK_TARGET_COUNT:-0}; i++ )); do
    target_var="REMOVE_WEBHOOK_TARGET_${i}"
    target="${!target_var}"
    if [[ "$target" != "$TARGET_NAME" ]]; then
        pvesh delete "/cluster/notifications/endpoints/webhook/$target" >/dev/null 2>&1 || true
    fi
done

matcher_args=(--mode all --target "$TARGET_NAME" --comment "$MATCHER_COMMENT")
for (( i=0; i<${MATCH_SEVERITY_COUNT:-0}; i++ )); do
    severity_var="MATCH_SEVERITY_${i}"
    matcher_args+=(--match-severity "${!severity_var}")
done

print_sub "Configuring notification matcher $MATCHER_NAME..."
# Update in place when the matcher already exists. The old delete-then-create left
# alerting silently disabled whenever the create failed, since the matcher was
# already gone by then.
if pvesh get "/cluster/notifications/matchers/$MATCHER_NAME" >/dev/null 2>&1; then
    pvesh set "/cluster/notifications/matchers/$MATCHER_NAME" "${matcher_args[@]}"
else
    pvesh create /cluster/notifications/matchers --name "$MATCHER_NAME" "${matcher_args[@]}"
fi

if [[ "${DISABLE_MAIL_TO_ROOT:-true}" == "true" ]]; then
    pvesh set /cluster/notifications/endpoints/sendmail/mail-to-root --disable 1 >/dev/null 2>&1 || true
fi

if [[ "${DISABLE_DEFAULT_MATCHER:-true}" == "true" ]]; then
    pvesh set /cluster/notifications/matchers/default-matcher --disable 1 >/dev/null 2>&1 || true
fi

print_ok "PVE notifications configured"
