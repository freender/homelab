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
    print_header() { echo "=== $* ==="; }
    print_error() { echo "    ✗ Error: $*" >&2; }
    backup_and_copy_if_changed() {
        local src="$1"
        local dst="$2"
        local label="${3:-$dst}"

        if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
            backup_config "$dst"
            cp "$src" "$dst"
            print_sub "Updated $label"
            return 0
        fi

        print_sub "$label unchanged; skipping update"
        return 1
    }
fi

if [[ ! -f "$BUILD_DIR/config" ]]; then
    print_error "missing config at $BUILD_DIR/config"
    exit 1
fi

print_header "Installing SSH config on $HOST"

mkdir -p ~/.ssh
chmod 700 ~/.ssh
mkdir -p ~/.ssh/sockets
chmod 700 ~/.ssh/sockets

backup_and_copy_if_changed "$BUILD_DIR/config" ~/.ssh/config "$HOME/.ssh/config" || true
chmod 600 ~/.ssh/config
