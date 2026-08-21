#!/bin/bash
# install-interfaces.sh - Install network interfaces config
# Usage: ./scripts/install-interfaces.sh [hostname]

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

require_file "$BUILD_DIR/interfaces" "$BUILD_DIR/interfaces" || exit 1

if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f /etc/network/interfaces ]] || ! cmp -s "$BUILD_DIR/interfaces" /etc/network/interfaces; then
    pending_diff=""
    if [[ -f /etc/network/interfaces ]]; then
        # diff exits 1 when files differ, which is the expected case here.
        pending_diff="$(diff -u /etc/network/interfaces "$BUILD_DIR/interfaces" || true)"
    fi

    print_sub "Backing up /etc/network/interfaces..."
    backup_config /etc/network/interfaces
    print_sub "Installing /etc/network/interfaces..."
    cp "$BUILD_DIR/interfaces" /etc/network/interfaces

    # Writing the file does not touch the running network: ifupdown2 reads it only on
    # `ifreload -a` or at boot. Warn loudly, because a silent write leaves the host
    # config-diverged until some unrelated reboot applies the change by surprise.
    # Deliberately do NOT auto-apply - reloading interfaces can drop the management
    # path, and this module has no way to recover a host it just cut off.
    print_warn "/etc/network/interfaces changed but is NOT live; running network is unchanged"
    if [[ -n "$pending_diff" ]]; then
        print_sub "Pending change:"
        printf '%s\n' "$pending_diff" | grep -E '^[+-][^+-]' | sed 's/^/        /' || true
    fi
    print_sub "Apply with: ifquery --check -a   then   ifreload -a   (or reboot)"
else
    print_sub "Network interfaces unchanged; skipping update"
fi
