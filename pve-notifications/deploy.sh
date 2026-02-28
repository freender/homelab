#!/bin/bash
# Deploy Proxmox notification configuration to PVE hosts
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
CONFIGS_DIR="$SCRIPT_DIR/configs"
ENV_FILE="$CONFIGS_DIR/telegram.env"
FALLBACK_ENV_FILE="$HOMELAB_ROOT/apcupsd/configs/telegram/telegram.env"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-notifications)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-notifications (not applicable to $1)"
    exit 0
fi

if [[ ! -f "$ENV_FILE" && -f "$FALLBACK_ENV_FILE" ]]; then
    ENV_FILE="$FALLBACK_ENV_FILE"
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: telegram env file not found!"
    echo "  cp pve-notifications/configs/telegram.env.example pve-notifications/configs/telegram.env"
    echo "  or provide apcupsd/configs/telegram/telegram.env"
    exit 1
fi

# shellcheck source=/dev/null
source "$ENV_FILE"

if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_CHATID:-}" ]]; then
    echo "ERROR: TELEGRAM_TOKEN and TELEGRAM_CHATID must be set in $ENV_FILE"
    exit 1
fi

deploy() {
    local host="$1"
    local host_type
    local build_dir="$BUILD_ROOT/$host"
    local token_b64 chatid_b64

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    if [[ "$host_type" != "pve" ]]; then
        print_warn "Skipping $host: pve-notifications only supports type: pve"
        return 0
    fi

    token_b64=$(printf '%s' "$TELEGRAM_TOKEN" | base64 | tr -d '\n')
    chatid_b64=$(printf '%s' "$TELEGRAM_CHATID" | base64 | tr -d '\n')

    prepare_build_dir "$build_dir"

    cp "$CONFIGS_DIR/notifications.cfg" "$build_dir/notifications.cfg"

    cat > "$build_dir/priv-notifications.cfg" <<EOF
webhook: Telegram
	secret name=bot_id,value=${token_b64}
	secret name=chat_id,value=${chatid_b64}
EOF

    print_sub "Comparing with remote configs..."
    diff_remote_config "$host" "$build_dir/notifications.cfg" "/etc/pve/notifications.cfg" || true
    diff_remote_config "$host" "$build_dir/priv-notifications.cfg" "/etc/pve/priv/notifications.cfg" || true

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-notifications/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-notifications && mkdir -p /tmp/homelab-pve-notifications/build /tmp/homelab-pve-notifications/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-notifications/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-notifications/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-notifications/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-notifications && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh '$host'"
}

deploy_init "PVE Notifications"
deploy_run deploy $HOSTS
deploy_finish
