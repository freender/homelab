#!/bin/bash
# install.sh - Install the riven PVE deploy webhook runner.

set -euo pipefail

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
SERVICE=homelab-pve-deploy-webhook.service
ENV_FILE=/etc/homelab/pve-deploy-webhook.env
RUNNER=/usr/local/sbin/homelab-pve-deploy-webhook

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

print_header "PVE Deploy Webhook"

changed=false
ic() {
    local rc=0
    install_if_changed "$@" || rc=$?
    if [[ $rc -eq 0 ]]; then
        changed=true
    fi
    [[ $rc -le 1 ]] || return "$rc"
}

print_action "Installing webhook runner"
ic "$BUILD_DIR/homelab-pve-deploy-webhook.py" "$RUNNER" 755 "$RUNNER"

print_action "Installing systemd unit"
ic "$BUILD_DIR/$SERVICE" "/etc/systemd/system/$SERVICE" 644 "$SERVICE"

if [[ -f "$BUILD_DIR/homelab-pve-deploy-webhook.env" ]]; then
    print_action "Installing webhook environment"
    mkdir -p /etc/homelab
    install -m 0640 -o root -g freender "$BUILD_DIR/homelab-pve-deploy-webhook.env" "$ENV_FILE"
    print_ok "Webhook environment installed"
elif [[ -f "$ENV_FILE" ]]; then
    print_sub "Webhook environment already present; skipping (not staged)"
else
    print_error "Webhook environment not staged and not present"
    exit 1
fi

systemctl daemon-reload
systemctl enable "$SERVICE"
if [[ "$changed" == "true" ]] || ! systemctl is-active --quiet "$SERVICE"; then
    systemctl restart "$SERVICE"
    print_ok "$SERVICE restarted"
else
    print_sub "$SERVICE already running"
fi

print_ok "pve-deploy-webhook deploy complete"
