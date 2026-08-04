#!/bin/bash
# install.sh - Install SSH config on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$BUILD_DIR/config" "$BUILD_DIR/config" || exit 1

print_header "Installing SSH config on $HOST"

mkdir -p ~/.ssh
chmod 700 ~/.ssh

rc=0
backup_and_copy_if_changed "$BUILD_DIR/config" ~/.ssh/config "$HOME/.ssh/config" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
chmod 600 ~/.ssh/config
