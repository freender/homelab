#!/bin/bash
# install.sh - Install PBS config sync script and timer
# Usage: ./scripts/install.sh [hostname] [schedule]

set -e

HOST=${1:-$(hostname)}
SCHEDULE=${2:-00:30}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
BACKUP_DIR="/var/backups/homelab/pbs-config-sync"
TELEGRAM_ENV_SOURCE="$SCRIPT_DIR/configs/telegram/telegram.env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    }
    print_sub() { echo "    $*"; }
    print_warn() { echo "    Warning: $*"; }
fi

if [[ ! -d "$BUILD_DIR" ]]; then
    echo "Error: Missing build directory $BUILD_DIR"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

print_sub "Installing rsync..."
apt-get update >/dev/null
apt-get install -y rsync >/dev/null

print_sub "Backing up existing deployed files..."
backup_config /root/backup-config.sh
backup_config /etc/systemd/system/backup-config.service
backup_config /etc/systemd/system/backup-config-notify@.service
backup_config /etc/systemd/system/backup-config.timer

print_sub "Deploying backup-config script..."
cp "$BUILD_DIR/backup-config.sh" /root/backup-config.sh
chmod 700 /root/backup-config.sh

print_sub "Deploying telegram env for notifications..."
if [[ ! -f "$TELEGRAM_ENV_SOURCE" ]]; then
    echo "Error: missing staged telegram env file at $TELEGRAM_ENV_SOURCE" >&2
    exit 1
fi
mkdir -p /etc/homelab
cp "$TELEGRAM_ENV_SOURCE" /etc/homelab/telegram.env
chmod 600 /etc/homelab/telegram.env

print_sub "Deploying systemd units..."
cp "$BUILD_DIR/backup-config.service" /etc/systemd/system/backup-config.service
cp "$BUILD_DIR/backup-config-notify@.service" /etc/systemd/system/backup-config-notify@.service
cp "$BUILD_DIR/backup-config.timer" /etc/systemd/system/backup-config.timer

print_sub "Updating timer schedule to $SCHEDULE..."
sed -i "s/^OnCalendar=.*/OnCalendar=*-*-* ${SCHEDULE}:00/" /etc/systemd/system/backup-config.timer

if [[ ! -x /etc/apcupsd/telegram/telegram.sh ]]; then
    print_warn "/etc/apcupsd/telegram/telegram.sh not found or not executable"
    print_warn "Failure notifications will not send until apcupsd telegram script is present"
fi

print_sub "Reloading systemd and enabling timer..."
systemctl daemon-reload
systemctl enable --now backup-config.timer

print_sub "Verifying timer state..."
systemctl is-enabled backup-config.timer >/dev/null
systemctl is-active backup-config.timer >/dev/null
systemctl status backup-config.timer --no-pager -n 3 | sed 's/^/    /'

print_sub "Running initial sync..."
/root/backup-config.sh

print_sub "PBS config sync deployment complete"
