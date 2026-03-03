#!/bin/bash
# install-interfaces.sh - Install network interfaces config
# Usage: ./scripts/install-interfaces.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub() { echo "    $*"; }
fi

if [[ ! -f "$BUILD_DIR/interfaces" ]]; then
    echo "Error: Missing interfaces file at $BUILD_DIR/interfaces" >&2
    exit 1
fi

print_sub "Backing up /etc/network/interfaces..."
backup_config /etc/network/interfaces
print_sub "Installing /etc/network/interfaces..."
cp "$BUILD_DIR/interfaces" /etc/network/interfaces
