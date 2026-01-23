#!/bin/bash
# lib/utils.sh - Lightweight utilities for remote hosts
# Sources: print.sh
# No external dependencies (no yq, no SSH, no downloads)

UTILS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [[ -f "${UTILS_DIR}/print.sh" ]]; then
    source "${UTILS_DIR}/print.sh"
else
    print_header() { echo "=== $* ==="; }
    print_action() { echo "==> $*"; }
    print_sub()    { echo "    $*"; }
    print_ok()     { echo "    ✓ $*"; }
    print_warn()   { echo "    ✗ Warning: $*"; }
fi

# Backup a file or directory
# Usage: backup_config /etc/foo/bar.conf
# Creates: /etc/foo/bar.conf.bak.YYYYMMDDHHmmss
backup_config() {
    local path="$1"
    [[ -e "$path" ]] || return 0

    local backup="${path}.bak.$(date +%Y%m%d%H%M%S)"
    if [[ -d "$path" ]]; then
        cp -r "$path" "$backup"
    else
        cp "$path" "$backup"
    fi
}

# Enable and start a systemd service
# Usage: enable_service service
enable_service() {
    local service="$1"
    systemctl enable --now "$service"
}

# Verify a systemd service is running
# Usage: verify_service service
# Returns: 0 if active, 1 if not
verify_service() {
    local service="$1"

    if systemctl is-active --quiet "$service" 2>/dev/null; then
        print_ok "$service running"
        return 0
    else
        print_warn "$service not running"
        systemctl status "$service" --no-pager -l 2>/dev/null || true
        return 1
    fi
}
