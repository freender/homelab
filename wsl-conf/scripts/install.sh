#!/bin/bash
# install.sh - Install /etc/wsl.conf on a WSL2 host
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

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/wsl.conf" "$BUILD_DIR/wsl.conf" || exit 1

print_header "WSL Conf"

# Safety guard: refuse to touch /etc/wsl.conf on a host that isn't actually
# WSL. A hosts.conf mistake (e.g. reusing type: ubuntu on a real VM/LXC)
# could otherwise point this module at the wrong host.
if [[ ! -e /proc/sys/fs/binfmt_misc/WSLInterop ]] && ! grep -qi microsoft /proc/version 2>/dev/null; then
    print_error "This does not look like a WSL host (no WSLInterop, no microsoft kernel signature); refusing to install /etc/wsl.conf"
    exit 1
fi

print_action "/etc/wsl.conf"
rc=0
backup_and_install_if_changed "$BUILD_DIR/wsl.conf" /etc/wsl.conf 644 "/etc/wsl.conf" || rc=$?
if [[ $rc -eq 0 ]]; then
    print_ok "/etc/wsl.conf updated"
    print_warn "Run 'wsl --shutdown' from Windows PowerShell to apply (not from inside this session)"
elif [[ $rc -eq 1 ]]; then
    print_sub "/etc/wsl.conf unchanged"
else
    exit "$rc"
fi

print_header "WSL Conf Complete"
