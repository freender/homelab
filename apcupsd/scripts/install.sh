#!/bin/bash
# install.sh - Install apcupsd on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
ENV_FILE="$BUILD_DIR/env"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

ROLE="unknown"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
fi

echo "=== Installing apcupsd $ROLE on $HOST ==="

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

# Install package if needed
if ! command -v apcupsd >/dev/null 2>&1; then
    echo "Installing apcupsd package..."
    apt update && apt install -y apcupsd
fi

apcupsd_changed=false

echo "Copying configuration files..."
if backup_and_copy_if_changed "$BUILD_DIR/apcupsd.conf" /etc/apcupsd/apcupsd.conf "apcupsd.conf"; then
    apcupsd_changed=true
else
    rc=$?
    [[ $rc -eq 1 ]] || exit "$rc"
fi

if copy_if_changed "$BUILD_DIR/doshutdown" /etc/apcupsd/doshutdown "doshutdown"; then
    apcupsd_changed=true
else
    rc=$?
    [[ $rc -eq 1 ]] || exit "$rc"
fi

# Set permissions
chmod +x /etc/apcupsd/doshutdown
chmod 644 /etc/apcupsd/apcupsd.conf
chmod +x /etc/apcupsd/apccontrol

# Enable apcupsd
if [[ -f /etc/default/apcupsd ]]; then
    sed -i 's/^ISCONFIGURED=no/ISCONFIGURED=yes/' /etc/default/apcupsd
fi

# Enable and start service
systemctl enable apcupsd
if [[ "$apcupsd_changed" == "true" ]]; then
    systemctl stop apcupsd 2>/dev/null || true
    systemctl start apcupsd
else
    echo "No apcupsd content changes detected; restart skipped"
fi

echo ""
echo "=== apcupsd $ROLE installed on $HOST ==="
echo ""
apcaccess status 2>/dev/null | grep -E "STATUS|MODEL|TIMELEFT|BCHARGE" || echo "Waiting for UPS connection..."
