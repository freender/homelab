#!/bin/bash
# install.sh - Install pve exporters on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub()   { echo "    $*"; }
    print_error() { echo "    ✗ Error: $*" >&2; }
    file_needs_update() {
        local src="$1" dst="$2"
        [[ -f "$src" ]] || return 2
        [[ ! -f "$dst" ]] && return 0
        [[ "$FORCE_UPDATE" == "true" ]] && return 0
        cmp -s "$src" "$dst" && return 1
        return 0
    }
    copy_if_changed() {
        local src="$1" dst="$2" label="${3:-$2}"
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
        local src="$1" dst="$2" label="${3:-$2}"
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

if [[ ! -d "$BUILD_DIR/configs" ]]; then
    print_error "Build directory not found: $BUILD_DIR/configs"
    exit 1
fi

NODE_ENV_SRC="$BUILD_DIR/configs/node-exporter.defaults"
SMART_ENV_SRC="$BUILD_DIR/configs/smartctl-exporter.defaults"
SMART_SVC_SRC="$BUILD_DIR/configs/smartctl-exporter.service"
APC_BIN_SRC="$BUILD_DIR/configs/apcupsd-exporter.py"
APC_ENV_SRC="$BUILD_DIR/configs/apcupsd-exporter.env"
APC_SVC_SRC="$BUILD_DIR/configs/apcupsd-exporter.service"

# Install packages only when missing
missing_pkgs=()
command -v prometheus-node-exporter &>/dev/null || missing_pkgs+=(prometheus-node-exporter)
command -v smartctl &>/dev/null              || missing_pkgs+=(smartmontools)
command -v python3 &>/dev/null               || missing_pkgs+=(python3)
command -v curl &>/dev/null                  || missing_pkgs+=(curl)
command -v tar &>/dev/null                   || missing_pkgs+=(tar)
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    print_sub "Installing packages: ${missing_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing_pkgs[@]}"
else
    print_sub "All required packages already installed"
fi

mkdir -p /etc/default
backup_and_copy_if_changed "$NODE_ENV_SRC" /etc/default/prometheus-node-exporter
backup_and_copy_if_changed "$SMART_ENV_SRC" /etc/default/smartctl-exporter
backup_and_copy_if_changed "$SMART_SVC_SRC" /etc/systemd/system/smartctl-exporter.service

if [[ -f "$APC_BIN_SRC" && -f "$APC_ENV_SRC" && -f "$APC_SVC_SRC" ]]; then
    if file_needs_update "$APC_BIN_SRC" /usr/local/bin/apcupsd-exporter; then
        backup_config /usr/local/bin/apcupsd-exporter
        install -m 755 "$APC_BIN_SRC" /usr/local/bin/apcupsd-exporter
        print_sub "Updated apcupsd-exporter"
    else
        print_sub "apcupsd-exporter unchanged; skipping update"
    fi
    backup_and_copy_if_changed "$APC_ENV_SRC" /etc/default/apcupsd-exporter
    backup_and_copy_if_changed "$APC_SVC_SRC" /etc/systemd/system/apcupsd-exporter.service
else
    systemctl disable --now apcupsd-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter
fi

# Install smartctl_exporter binary (version-aware)
# shellcheck source=/etc/default/smartctl-exporter
source /etc/default/smartctl-exporter
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
    amd64) ARCH_TAG="amd64" ;;
    arm64) ARCH_TAG="arm64" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

SMART_BIN="/usr/local/bin/smartctl_exporter"
SMART_URL="https://github.com/prometheus-community/smartctl_exporter/releases/download/v${SMARTCTL_EXPORTER_VERSION}/smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-${ARCH_TAG}.tar.gz"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Detect installed version
installed_version=""
if [[ -x "$SMART_BIN" ]]; then
    installed_version=$("$SMART_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
fi

if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -x "$SMART_BIN" ]] || [[ "$installed_version" != "$SMARTCTL_EXPORTER_VERSION" ]]; then
    print_sub "Installing smartctl_exporter v${SMARTCTL_EXPORTER_VERSION} (was: ${installed_version:-none})"
    curl -fsSL "$SMART_URL" -o "$TMP_DIR/smartctl-exporter.tar.gz"
    tar -xzf "$TMP_DIR/smartctl-exporter.tar.gz" -C "$TMP_DIR"
    # Stop service before replacing binary to avoid "Text file busy"
    systemctl stop smartctl-exporter 2>/dev/null || true
    cp "$TMP_DIR/smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-${ARCH_TAG}/smartctl_exporter" "$SMART_BIN"
    chmod 755 "$SMART_BIN"
else
    print_sub "smartctl_exporter v${SMARTCTL_EXPORTER_VERSION} already installed"
fi

systemctl daemon-reload
systemctl enable --now prometheus-node-exporter
systemctl enable --now smartctl-exporter
if [[ -f "$APC_BIN_SRC" && -f "$APC_ENV_SRC" && -f "$APC_SVC_SRC" ]]; then
    systemctl enable --now apcupsd-exporter
    systemctl is-active --quiet apcupsd-exporter
fi
systemctl is-active --quiet prometheus-node-exporter
systemctl is-active --quiet smartctl-exporter
