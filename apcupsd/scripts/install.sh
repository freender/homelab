#!/bin/bash
# install.sh - Install apcupsd on target host
# Usage: ./scripts/install.sh [hostname]
# If no hostname provided, uses current hostname

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOSTS_CONF="$SCRIPT_DIR/hosts.conf"

# Get host role from apcupsd/hosts.conf (data-driven)
if [[ ! -f "$HOSTS_CONF" ]]; then
    echo "Error: apcupsd/hosts.conf not found at $HOSTS_CONF"
    exit 1
fi

get_host_value() {
    local target="$1"
    local key="$2"
    local in_host=0
    local line

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line//[[:space:]]/}" ]] && continue

        if [[ "$line" =~ ^[A-Za-z0-9._-]+:[[:space:]]*$ ]]; then
            if [[ "${line%%:*}" == "$target" ]]; then
                in_host=1
            else
                in_host=0
            fi
            continue
        fi

        if [[ $in_host -eq 1 && "$line" =~ ^[[:space:]]+${key}:[[:space:]]*.*$ ]]; then
            local value="${line#*:}"
            value="${value%"${value##*[![:space:]]}"}"
            value="${value#"${value%%[![:space:]]*}"}"
            if [[ "$value" =~ ^".*"$ ]]; then
                value="${value:1:${#value}-2}"
            elif [[ "$value" =~ ^'.*'$ ]]; then
                value="${value:1:${#value}-2}"
            fi
            echo "$value"
            return 0
        fi
    done < "$HOSTS_CONF"

    return 1
}

ROLE=$(get_host_value "$HOST" "ups.role")
if [[ -z "$ROLE" ]]; then
    echo "Error: ups.role missing for $HOST in apcupsd/hosts.conf"
    exit 1
fi

echo "=== Installing apcupsd $ROLE on $HOST ==="

RENDERED_DIR="$SCRIPT_DIR/$HOST"

# Check if rendered configs exist
if [[ ! -d "$RENDERED_DIR" ]]; then
    echo "Error: Rendered config directory not found: $RENDERED_DIR"
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
cp "$RENDERED_DIR/apcupsd.conf" /etc/apcupsd/
cp "$RENDERED_DIR/doshutdown" /etc/apcupsd/
# Use shared notify
cp "$SCRIPT_DIR/shared/apcupsd.notify" /etc/apcupsd/

# Setup telegram
mkdir -p /etc/apcupsd/telegram
cp "$SCRIPT_DIR/telegram/telegram.sh" /etc/apcupsd/telegram/

# Copy telegram.env from temp directory
ENV_FILE="/etc/apcupsd/telegram/telegram.env"
if [[ -f "$SCRIPT_DIR/telegram/telegram.env" ]]; then
    cp "$SCRIPT_DIR/telegram/telegram.env" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown root:root "$ENV_FILE"
    echo "Telegram credentials installed."
else
    echo "ERROR: Missing $SCRIPT_DIR/telegram/telegram.env"
    echo "This should have been copied during deployment."
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
