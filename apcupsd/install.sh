#!/bin/bash
# install.sh - Install apcupsd on target host
# Usage: ./install.sh [hostname]
# If no hostname provided, uses current hostname

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Validate host
case $HOST in
    bray|xur)
        TYPE="master"
        ;;
    ace|clovis)
        TYPE="slave"
        ;;
    *)
        echo "Unknown host: $HOST"
        echo "Valid hosts: ace, bray, clovis, xur"
        exit 1
        ;;
esac

echo "=== Installing apcupsd $TYPE on $HOST ==="

# Check if config exists
if [[ ! -d "$SCRIPT_DIR/configs/$HOST" ]]; then
    echo "Error: Config directory not found: $SCRIPT_DIR/configs/$HOST"
    exit 1
fi

# Install package if needed
if ! which apcupsd >/dev/null 2>&1; then
    echo "Installing apcupsd package..."
    apt update && apt install -y apcupsd
fi

# Stop service if running
systemctl stop apcupsd 2>/dev/null || true

# Backup existing config
if [[ -f /etc/apcupsd/apcupsd.conf ]]; then
    cp /etc/apcupsd/apcupsd.conf /etc/apcupsd/apcupsd.conf.bak.$(date +%Y%m%d%H%M%S)
fi

# Copy configs
echo "Copying configuration files..."
cp "$SCRIPT_DIR/configs/$HOST/apcupsd.conf" /etc/apcupsd/
cp "$SCRIPT_DIR/configs/$HOST/doshutdown" /etc/apcupsd/
# Use host-specific notify if available, fallback to shared
if [[ -f "$SCRIPT_DIR/configs/$HOST/apcupsd.notify" ]]; then
  cp "$SCRIPT_DIR/configs/$HOST/apcupsd.notify" /etc/apcupsd/
else
  cp "$SCRIPT_DIR/shared/apcupsd.notify" /etc/apcupsd/
fi

# Setup telegram
mkdir -p /etc/apcupsd/telegram
cp "$SCRIPT_DIR/shared/telegram/telegram.sh" /etc/apcupsd/telegram/

ENV_FILE="/etc/apcupsd/telegram/telegram.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing $ENV_FILE. Create it with TELEGRAM_TOKEN and TELEGRAM_CHATID."
    exit 1
fi

# Set permissions
chmod +x /etc/apcupsd/doshutdown
chmod +x /etc/apcupsd/telegram/telegram.sh
chmod +x /etc/apcupsd/apcupsd.notify
chmod 644 /etc/apcupsd/apcupsd.conf
chmod +x /etc/apcupsd/apccontrol

# Enable apcupsd
if [[ -f /etc/default/apcupsd ]]; then
    sed -i 's/^ISCONFIGURED=no/ISCONFIGURED=yes/' /etc/default/apcupsd
fi

# Enable and start service
systemctl enable apcupsd
systemctl start apcupsd

echo ""
echo "=== apcupsd $TYPE installed on $HOST ==="
echo ""
apcaccess status 2>/dev/null | grep -E "STATUS|MODEL|TIMELEFT|BCHARGE" || echo "Waiting for UPS connection..."
echo ""
echo "Test Telegram: /etc/apcupsd/telegram/telegram.sh -s 'Test' -d 'Test from $HOST'"
