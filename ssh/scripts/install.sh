#!/bin/bash
# install.sh - Install SSH config on target host
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
    print_sub() { echo "    $*"; }
fi

if [[ ! -f "$BUILD_DIR/config" ]]; then
    echo "Error: Missing config at $BUILD_DIR/config"
    exit 1
fi

mkdir -p ~/.ssh
chmod 700 ~/.ssh
if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f ~/.ssh/config ]] || ! cmp -s "$BUILD_DIR/config" ~/.ssh/config; then
    backup_config ~/.ssh/config
    cp "$BUILD_DIR/config" ~/.ssh/config
    print_sub "Updated ~/.ssh/config"
else
    print_sub "$HOME/.ssh/config unchanged; skipping update"
fi
chmod 600 ~/.ssh/config
