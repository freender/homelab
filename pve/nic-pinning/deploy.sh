#!/bin/bash
# Deploy NIC pinning configs to PVE nodes
# Usage: ./deploy.sh [ace|bray|clovis|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS="${@:-ace bray clovis}"
if [[ "$1" == "all" ]]; then
    HOSTS="ace bray clovis"
fi

echo "==> Deploying NIC Pinning"
echo "    Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    echo "==> Deploying to $host..."

    if [[ ! -d "$SCRIPT_DIR/$host" ]]; then
        echo "    ✗ Error: No config found for node: $host"
        echo "    Available: $(ls -d "$SCRIPT_DIR"/*/ 2>/dev/null | xargs -n1 basename | tr n  )"
        continue
    fi

    # Ensure target directory exists
    ssh "$host" "mkdir -p /usr/local/lib/systemd/network"

    # Copy link files
    echo "    Copying .link files..."
    scp "$SCRIPT_DIR/$host"/*.link "$host":/usr/local/lib/systemd/network/

    # Update initramfs
    echo "    Updating initramfs..."
    ssh "$host" "update-initramfs -u -k all"

    echo "    ✓ Deployed to $host (reboot required)"
    echo ""
done

echo "==> Deployment complete!"
echo ""
echo "Reboot nodes to apply changes:"
echo "  ssh <node> reboot"
