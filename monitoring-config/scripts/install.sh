#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:?host argument is required}"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
CONFIG_DIR="$SCRIPT_DIR/configs"
VMAGENT_CONFIG="$CONFIG_DIR/scrape.yml"
VMAGENT_DEST="/mnt/cache/appdata/vmagent/scrape.yml"
ALERTMANAGER_CONFIG="$CONFIG_DIR/alertmanager.yml.tpl"
ALERTMANAGER_DEST="/mnt/cache/appdata/alertmanager/alertmanager.yml.tpl"
ALERTMANAGER_COMPOSE="/mnt/cache/appdata/alertmanager/compose.yml"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$BUILD_DIR/env" "monitoring config environment" || exit 1
# shellcheck source=/dev/null
source "$BUILD_DIR/env"
require_env ALERTMANAGER_ENABLED VMAGENT_CONTAINER || exit 1
require_file "$VMAGENT_CONFIG" "vmagent scrape config" || exit 1
require_file "$VMAGENT_DEST" "live vmagent scrape config" || exit 1

if [[ "$ALERTMANAGER_ENABLED" != "true" && "$ALERTMANAGER_ENABLED" != "false" ]]; then
    print_error "ALERTMANAGER_ENABLED must be true or false"
    exit 1
fi

if ! docker inspect "$VMAGENT_CONTAINER" >/dev/null 2>&1; then
    print_error "vmagent container is missing: $VMAGENT_CONTAINER"
    exit 1
fi

vmagent_image="$(docker inspect --format '{{.Config.Image}}' "$VMAGENT_CONTAINER")"
print_sub "Validating staged vmagent scrape config..."
docker run --rm -v "$VMAGENT_CONFIG:/etc/vmagent/scrape.yml:ro" "$vmagent_image" \
    -promscrape.config=/etc/vmagent/scrape.yml -promscrape.config.dryRun

vmagent_rc=0
copy_if_changed "$VMAGENT_CONFIG" "$VMAGENT_DEST" "vmagent scrape config" || vmagent_rc=$?
[[ $vmagent_rc -eq 0 || $vmagent_rc -eq 1 ]] || exit "$vmagent_rc"
if [[ $vmagent_rc -eq 0 ]]; then
    print_sub "Reloading $VMAGENT_CONTAINER..."
    docker exec "$VMAGENT_CONTAINER" wget -qO- --post-data='' http://127.0.0.1:8429/-/reload >/dev/null
    print_ok "$VMAGENT_CONTAINER reloaded"
else
    print_ok "vmagent scrape config unchanged"
fi

if [[ "$ALERTMANAGER_ENABLED" != "true" ]]; then
    exit 0
fi

require_file "$ALERTMANAGER_CONFIG" "Alertmanager config template" || exit 1
require_file "$ALERTMANAGER_DEST" "live Alertmanager config template" || exit 1
require_file "$ALERTMANAGER_COMPOSE" "Alertmanager compose file" || exit 1
if ! docker inspect alertmanager >/dev/null 2>&1; then
    print_error "Alertmanager container is missing"
    exit 1
fi

validation_dir="$(mktemp -d)"
trap 'rm -rf "$validation_dir"' EXIT
# The healthcheck URL uses | as the sed delimiter because it contains slashes.
# A placeholder host is enough: amtool validates URL syntax without connecting.
sed \
    -e 's/__TELEGRAM_CHATID__/123456/g' \
    -e 's/__TELEGRAM_CHATID_PLEX__/654321/g' \
    -e 's|__HEALTHCHECK_URL__|https://example.net/ping/validation|g' \
    "$ALERTMANAGER_CONFIG" > "$validation_dir/alertmanager.yml"
printf x > "$validation_dir/telegram_token"
printf x > "$validation_dir/telegram_token_plex"

alertmanager_image="$(docker inspect --format '{{.Config.Image}}' alertmanager)"
print_sub "Validating staged Alertmanager config..."
docker run --rm \
    -v "$validation_dir/alertmanager.yml:/config/alertmanager.yml:ro" \
    -v "$validation_dir/telegram_token:/tmp/telegram_token:ro" \
    -v "$validation_dir/telegram_token_plex:/tmp/telegram_token_plex:ro" \
    --entrypoint /bin/amtool "$alertmanager_image" \
    check-config /config/alertmanager.yml

alertmanager_rc=0
copy_if_changed "$ALERTMANAGER_CONFIG" "$ALERTMANAGER_DEST" \
    "Alertmanager config template" || alertmanager_rc=$?
[[ $alertmanager_rc -eq 0 || $alertmanager_rc -eq 1 ]] || exit "$alertmanager_rc"
if [[ $alertmanager_rc -eq 0 ]]; then
    print_sub "Recreating Alertmanager to render the updated template..."
    docker compose -f "$ALERTMANAGER_COMPOSE" up -d --force-recreate alertmanager
    docker exec alertmanager /bin/amtool check-config /tmp/alertmanager.yml
    print_ok "Alertmanager recreated"
else
    print_ok "Alertmanager config template unchanged"
fi
