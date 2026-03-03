#!/bin/bash
# install.sh - Install apcupsd on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
CONFIGS_DIR="$SCRIPT_DIR/configs"
ENV_FILE="$BUILD_DIR/env"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub() { echo "    $*"; }
    file_needs_update() {
        local src="$1"
        local dst="$2"
        [[ -f "$src" ]] || return 2
        [[ ! -f "$dst" ]] && return 0
        [[ "$FORCE_UPDATE" == "true" ]] && return 0
        cmp -s "$src" "$dst" && return 1
        return 0
    }
    copy_if_changed() {
        local src="$1"
        local dst="$2"
        local label="${3:-$dst}"
        if file_needs_update "$src" "$dst"; then
            cp "$src" "$dst"
            print_sub "Updated $label"
            return 0
        fi
        local rc=$?
        [[ $rc -eq 1 ]] && { print_sub "$label unchanged; skipping update"; return 1; }
        return "$rc"
    }
    backup_and_copy_if_changed() {
        local src="$1"
        local dst="$2"
        local label="${3:-$dst}"
        if file_needs_update "$src" "$dst"; then
            backup_config "$dst"
            cp "$src" "$dst"
            print_sub "Updated $label"
            return 0
        fi
        local rc=$?
        [[ $rc -eq 1 ]] && { print_sub "$label unchanged; skipping update"; return 1; }
        return "$rc"
    }
fi

ROLE="unknown"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
fi

echo "=== Installing apcupsd $ROLE on $HOST ==="

if [[ ! -d "$BUILD_DIR" ]]; then
    echo "Error: Rendered config directory not found: $BUILD_DIR"
    exit 1
fi

if [[ ! -f "$CONFIGS_DIR/shared/apcupsd.notify" ]]; then
    echo "Error: Missing $CONFIGS_DIR/shared/apcupsd.notify"
    exit 1
fi

if [[ ! -f "$CONFIGS_DIR/telegram/telegram.sh" ]]; then
    echo "Error: Missing $CONFIGS_DIR/telegram/telegram.sh"
    exit 1
fi

if [[ ! -f "$CONFIGS_DIR/telegram/telegram.env" ]]; then
    echo "Error: Missing $CONFIGS_DIR/telegram/telegram.env"
    exit 1
fi

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

if copy_if_changed "$CONFIGS_DIR/shared/apcupsd.notify" /etc/apcupsd/apcupsd.notify "apcupsd.notify"; then
    apcupsd_changed=true
else
    rc=$?
    [[ $rc -eq 1 ]] || exit "$rc"
fi

# Setup telegram
mkdir -p /etc/apcupsd/telegram
if copy_if_changed "$CONFIGS_DIR/telegram/telegram.sh" /etc/apcupsd/telegram/telegram.sh "telegram.sh"; then
    apcupsd_changed=true
else
    rc=$?
    [[ $rc -eq 1 ]] || exit "$rc"
fi

ENV_FILE_DEST="/etc/apcupsd/telegram/telegram.env"
if copy_if_changed "$CONFIGS_DIR/telegram/telegram.env" "$ENV_FILE_DEST" "telegram.env"; then
    apcupsd_changed=true
else
    rc=$?
    [[ $rc -eq 1 ]] || exit "$rc"
fi
chmod 600 "$ENV_FILE_DEST"
chown root:root "$ENV_FILE_DEST"
echo "Telegram credentials installed."

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
echo ""
echo "Test Telegram: /etc/apcupsd/telegram/telegram.sh -s 'Test' -d 'Test from $HOST'"
