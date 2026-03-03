#!/bin/bash
# install.sh - Run apt dist-upgrade and optionally clean old kernels

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/build/env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
fi

CLEAN_KERNELS="false"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
fi

print_sub "Updating package metadata..."
apt update &>/dev/null || print_warn "apt update failed"

print_sub "Running dist-upgrade..."
apt -y dist-upgrade &>/dev/null || print_warn "apt dist-upgrade failed"

if [[ "$CLEAN_KERNELS" == "true" ]]; then
    print_sub "Removing old kernels..."

    current_kernel=$(uname -r)
    print_sub "Current kernel: $current_kernel"

    # Find installed kernel packages that are NOT the currently running kernel
    old_kernels=$(dpkg -l 'linux-image-*' 'pve-kernel-*' 2>/dev/null \
        | awk '/^ii/ { print $2 }' \
        | grep -v "$current_kernel" \
        | grep -v 'linux-image-amd64' \
        | grep -v 'linux-image-generic' \
        | grep -v 'pve-kernel-helper' \
        || true)

    if [[ -z "$old_kernels" ]]; then
        print_sub "No old kernels to remove"
    else
        for pkg in $old_kernels; do
            print_sub "Removing $pkg"
        done
        # shellcheck disable=SC2086 # intentional word splitting, package names are controlled
        apt -y purge $old_kernels &>/dev/null || print_warn "kernel purge failed"
        apt -y autoremove &>/dev/null || print_warn "autoremove failed"
        update-grub 2>/dev/null || true
        print_sub "Old kernels removed"
    fi
fi
