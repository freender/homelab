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
    backup_config() { local path="$1"; [[ -e "$path" ]] || return 0; cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"; }
    print_sub() { echo "    $*"; }
fi

if [[ ! -d "$BUILD_DIR/configs" ]]; then
    echo "Error: Build directory not found: $BUILD_DIR/configs"
    exit 1
fi

apt-get update -qq
apt-get install -y -qq prometheus-node-exporter smartmontools python3 curl tar

NODE_ENV_SRC="$BUILD_DIR/configs/node-exporter.defaults"
SMART_ENV_SRC="$BUILD_DIR/configs/smartctl-exporter.defaults"
SMART_SVC_SRC="$BUILD_DIR/configs/smartctl-exporter.service"
APC_BIN_SRC="$BUILD_DIR/configs/apcupsd-exporter.py"
APC_ENV_SRC="$BUILD_DIR/configs/apcupsd-exporter.env"
APC_SVC_SRC="$BUILD_DIR/configs/apcupsd-exporter.service"

mkdir -p /etc/default
cp "$NODE_ENV_SRC" /etc/default/prometheus-node-exporter
cp "$SMART_ENV_SRC" /etc/default/smartctl-exporter
cp "$SMART_SVC_SRC" /etc/systemd/system/smartctl-exporter.service

if [[ -f "$APC_BIN_SRC" && -f "$APC_ENV_SRC" && -f "$APC_SVC_SRC" ]]; then
    install -m 755 "$APC_BIN_SRC" /usr/local/bin/apcupsd-exporter
    install -m 644 "$APC_ENV_SRC" /etc/default/apcupsd-exporter
    install -m 644 "$APC_SVC_SRC" /etc/systemd/system/apcupsd-exporter.service
else
    systemctl disable --now apcupsd-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter
fi

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
trap "rm -rf """ EXIT

if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -x "$SMART_BIN" ]]; then
    print_sub "Installing smartctl_exporter v${SMARTCTL_EXPORTER_VERSION}"
    curl -fsSL "$SMART_URL" -o "$TMP_DIR/smartctl-exporter.tar.gz"
    tar -xzf "$TMP_DIR/smartctl-exporter.tar.gz" -C "$TMP_DIR"
    cp "$TMP_DIR/smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-${ARCH_TAG}/smartctl_exporter" "$SMART_BIN"
    chmod 755 "$SMART_BIN"
else
    print_sub "smartctl_exporter already installed; use --force to re-install"
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
