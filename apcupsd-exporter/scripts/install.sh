#!/bin/bash

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ ! -d "$BUILD_DIR/configs" ]]; then
    echo "Error: Build directory not found: $BUILD_DIR/configs"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq python3
fi

install -m 755 "$BUILD_DIR/configs/apcupsd-exporter.py" /usr/local/bin/apcupsd-exporter
install -m 644 "$BUILD_DIR/configs/apcupsd-exporter.env" /etc/default/apcupsd-exporter
install -m 644 "$BUILD_DIR/configs/apcupsd-exporter.service" /etc/systemd/system/apcupsd-exporter.service

systemctl daemon-reload
systemctl enable --now apcupsd-exporter
systemctl is-active --quiet apcupsd-exporter
