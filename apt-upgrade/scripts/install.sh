#!/bin/bash
# install.sh - Install apt dist-upgrade service (and optionally a daily timer)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
FORCE_UPDATE=${FORCE_UPDATE:-false}

SERVICE_NAME="homelab-apt-dist-upgrade.service"
TIMER_NAME="homelab-apt-dist-upgrade.timer"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
TIMER_PATH="/etc/systemd/system/$TIMER_NAME"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

# Source env to get AUTOUPGRADE, SCHEDULE, and PAUSED
AUTOUPGRADE="false"
SCHEDULE="*-*-* 09:00:00"
PAUSED="false"
if [[ -f "$BUILD_DIR/env" ]]; then
    # shellcheck source=/dev/null
    source "$BUILD_DIR/env"
fi

# Always install the service unit (used both on-demand and by the timer)
local_changed=false
if copy_if_changed "$BUILD_DIR/service" "$SERVICE_PATH" "$SERVICE_NAME"; then
    local_changed=true
fi

if [[ "$local_changed" == true ]]; then
    systemctl daemon-reload
fi

# Paused: keep the service unit installed but stop/disable the timer and skip
# any on-demand upgrade. Flip apt-upgrade.paused back to false to resume.
if homelab_apply_pause "$PAUSED" "$TIMER_NAME"; then
    print_header "apt-upgrade paused"
    if [[ -f /var/run/reboot-required ]]; then
        print_warn "Reboot required on $(hostname)"
    fi
    exit 0
fi

if [[ "$AUTOUPGRADE" == "true" ]]; then
    # Install and enable the daily timer
    if copy_if_changed "$BUILD_DIR/timer" "$TIMER_PATH" "$TIMER_NAME"; then
        local_changed=true
    fi

    if [[ "$local_changed" == true ]]; then
        systemctl daemon-reload
    fi

    if ! systemctl is-enabled --quiet "$TIMER_NAME" 2>/dev/null; then
        systemctl enable --now "$TIMER_NAME" >/dev/null
        print_sub "Timer enabled at $SCHEDULE"
    else
        if [[ "$local_changed" == true ]]; then
            systemctl restart "$TIMER_NAME" >/dev/null
            print_sub "Timer restarted"
        else
            print_sub "Timer already enabled"
        fi
    fi

    systemctl list-timers --all --no-pager | grep -F "$TIMER_NAME" || true
else
    # autoupgrade not enabled: install service only, run upgrade now
    print_sub "Running apt upgrade now (autoupgrade not enabled)..."
    systemctl start "$SERVICE_NAME"
    print_sub "Upgrade complete"
fi

# Reboot check
if [[ -f /var/run/reboot-required ]]; then
    print_warn "Reboot required on $(hostname)"
fi
