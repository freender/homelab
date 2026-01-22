#!/bin/bash
# install.sh - Install SSH config on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ ! -f "$BUILD_DIR/config" ]]; then
    echo "Error: Missing config at $BUILD_DIR/config"
    exit 1
fi

mkdir -p ~/.ssh
chmod 700 ~/.ssh
cp "$BUILD_DIR/config" ~/.ssh/config
chmod 600 ~/.ssh/config
