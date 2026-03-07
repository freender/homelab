#!/bin/bash
# install.sh - Install apt dist-upgrade timer and service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/build/env"
SERVICE_NAME="homelab-apt-dist-upgrade.service"
TIMER_NAME="homelab-apt-dist-upgrade.timer"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
TIMER_PATH="/etc/systemd/system/$TIMER_NAME"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    Warning: $*"; }
fi

CLEANUP="false"
SCHEDULE="09:00"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
fi

print_sub "Installing $SERVICE_NAME"
cat > "$SERVICE_PATH" <<EOF2
[Unit]
Description=Homelab daily apt update and dist-upgrade
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/apt-get update
ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y dist-upgrade
EOF2

if [[ "$CLEANUP" == "true" ]]; then
cat >> "$SERVICE_PATH" <<'EOF2'
ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y autoremove
ExecStart=/usr/bin/apt-get -y autoclean
EOF2
fi

print_sub "Installing $TIMER_NAME at $SCHEDULE"
cat > "$TIMER_PATH" <<EOF2
[Unit]
Description=Run homelab daily apt update and dist-upgrade

[Timer]
OnCalendar=*-*-* ${SCHEDULE}:00
Persistent=true

[Install]
WantedBy=timers.target
EOF2

systemctl daemon-reload
systemctl enable --now "$TIMER_NAME" >/dev/null
print_sub "Timer enabled"
systemctl list-timers --all --no-pager | grep -F "$TIMER_NAME" || true
