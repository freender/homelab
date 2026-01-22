#!/bin/bash
# install.sh - Install network interfaces config
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ ! -f "$BUILD_DIR/interfaces" ]]; then
    echo "Error: Missing interfaces file at $BUILD_DIR/interfaces"
    exit 1
fi

cp /etc/network/interfaces /etc/network/interfaces.bak.$(date +%Y%m%d%H%M%S)
cp "$BUILD_DIR/interfaces" /etc/network/interfaces
