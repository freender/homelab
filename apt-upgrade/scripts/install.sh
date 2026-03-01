#!/bin/bash
# install.sh - Run apt dist-upgrade

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
fi

print_sub "Updating package metadata..."
apt update &>/dev/null || print_warn "apt update failed"

print_sub "Running dist-upgrade..."
apt -y dist-upgrade &>/dev/null || print_warn "apt dist-upgrade failed"
