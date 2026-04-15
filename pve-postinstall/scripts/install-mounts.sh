#!/usr/bin/env bash

set -euo pipefail

# Find and source library
LIB_DIR="$(dirname "$0")/../lib"
# shellcheck source=lib/utils.sh
if [[ -f "${LIB_DIR}/utils.sh" ]]; then
    source "${LIB_DIR}/utils.sh"
else
    # Fallback to current directory for local testing
    # shellcheck source=scripts/utils.sh
    source "$(dirname "$0")/utils.sh"
fi

print_header "Installing Disk Mounts"

MOUNTS=$1

if [[ -z "$MOUNTS" ]]; then
    print_sub "No mounts configured; skipping"
    exit 0
fi

# 1. Update /etc/fstab and create mountpoints
for mount in $MOUNTS; do
    LABEL=$(echo "$mount" | cut -d':' -f1)
    TARGET=$(echo "$mount" | cut -d':' -f2)

    print_action "Configuring mount for LABEL=${LABEL} at ${TARGET}"

    # Ensure target directory exists
    mkdir -p "${TARGET}"

    # Build fstab line
    FSTAB_LINE="LABEL=${LABEL} ${TARGET} auto nosuid,nodev,nofail,x-systemd.device-timeout=60,x-systemd.mount-timeout=60 0 2"

    # Add to fstab if not present
    if ! grep -q "LABEL=${LABEL}" /etc/fstab && ! grep -q "${TARGET}" /etc/fstab; then
        echo "${FSTAB_LINE}" >> /etc/fstab
        print_ok "Added LABEL=${LABEL} to /etc/fstab"
    else
        # If it's already there, just verify it matches (optional, for now just skip)
        print_sub "LABEL=${LABEL} already in /etc/fstab; skipping"
    fi
done

# 2. Mount all
print_action "Mounting all filesystems..."
systemctl daemon-reload
mount -a
print_ok "Mounts complete"
