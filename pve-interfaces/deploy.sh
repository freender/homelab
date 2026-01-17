#!/bin/bash
# Deploy network interfaces config to PVE nodes
# Usage: ./deploy.sh [ace|bray|clovis|xur|all]

set -e

# Supported hosts for this module
SUPPORTED_HOSTS=("ace" "bray" "clovis")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping pve-interfaces (not applicable to $1)"
        exit 0
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS="${@:-ace bray clovis xur}"
HOSTS="${@:-ace bray clovis xur}"
if [[ "$1" == "all" ]]; then
    HOSTS="ace bray clovis xur"
fi

echo "==> Deploying Network Interfaces"
echo "    Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    echo "==> Deploying to $host..."

    if [[ ! -f "$SCRIPT_DIR/$host/interfaces" ]]; then
        echo "    ✗ Error: No config found for node: $host"
        echo "    Available: $(ls -d "$SCRIPT_DIR"/*/ 2>/dev/null | xargs -n1 basename | tr n  )"
        continue
    fi

    # Backup existing config
    echo "    Backing up existing config..."
    ssh "$host" "cp /etc/network/interfaces /etc/network/interfaces.bak.$(date +%Y%m%d%H%M%S)"

    # Copy new config
    echo "    Copying interfaces file..."
    scp "$SCRIPT_DIR/$host/interfaces" "$host":/tmp/interfaces
    ssh "$host" "mv /tmp/interfaces /etc/network/interfaces && chmod 644 /etc/network/interfaces"

    echo "    ✓ Deployed to $host (reboot or ifreload required)"
    echo ""
done

echo "==> Deployment complete!"
echo ""
echo "Apply changes:"
echo "  ssh <node> ifreload -a   # Apply without reboot (may disrupt connections)"
echo "  ssh <node> reboot        # Or reboot to apply safely"
