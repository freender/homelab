#!/bin/bash
# install-config-backup.sh - Install PVE cluster config backup timer

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

BACKUP_SCRIPT="$BUILD_DIR/pve-config-backup.sh"
SERVICE_UNIT="$BUILD_DIR/homelab-pve-config-backup.service"
TIMER_UNIT="$BUILD_DIR/homelab-pve-config-backup.timer"
ENV_FILE_SOURCE="$BUILD_DIR/pbs.env"

for required in "$BACKUP_SCRIPT" "$SERVICE_UNIT" "$TIMER_UNIT" "$ENV_FILE_SOURCE"; do
    if [[ ! -f "$required" ]]; then
        print_error "Missing required file: $required"
        exit 1
    fi
done

if ! command -v proxmox-backup-client >/dev/null 2>&1; then
    print_error "proxmox-backup-client not found"
    exit 1
fi

print_sub "Installing backup script and units..."
install -m 700 "$BACKUP_SCRIPT" /root/pve-config-backup.sh
install -d -m 700 /etc/homelab
install -m 600 "$ENV_FILE_SOURCE" /etc/homelab/pve-config-backup.env
if systemctl is-enabled --quiet pve-config-backup.timer 2>/dev/null; then
    systemctl disable --now pve-config-backup.timer
    print_sub "Retired pve-config-backup.timer disabled"
elif systemctl is-active --quiet pve-config-backup.timer 2>/dev/null; then
    systemctl stop pve-config-backup.timer
    print_sub "Retired pve-config-backup.timer stopped"
fi
rm -f /etc/systemd/system/pve-config-backup.service /etc/systemd/system/pve-config-backup.timer
install -m 644 "$SERVICE_UNIT" /etc/systemd/system/homelab-pve-config-backup.service
install -m 644 "$TIMER_UNIT" /etc/systemd/system/homelab-pve-config-backup.timer

print_sub "Reloading systemd and enabling timer..."
systemctl daemon-reload
systemctl enable --now homelab-pve-config-backup.timer

if systemctl is-enabled --quiet homelab-pve-config-backup.timer; then
    print_sub "Timer enabled"
else
    print_error "Failed to enable timer"
    exit 1
fi

if systemctl is-active --quiet homelab-pve-config-backup.timer; then
    print_sub "Timer active"
else
    print_error "Timer not active"
    exit 1
fi

print_sub "Running initial backup now..."
systemctl start homelab-pve-config-backup.service

print_sub "Next run:"
systemctl list-timers homelab-pve-config-backup.timer --no-pager --all || true
