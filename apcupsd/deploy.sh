#!/bin/bash
# deploy.sh - Deploy apcupsd config to remote hosts via SSH
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

set -e

DEFAULT_HOSTS="xur ace clovis bray"
HOSTS="$*"

if [[ -z "$HOSTS" ]] || [[ "$1" == "all" ]]; then
    HOSTS="$DEFAULT_HOSTS"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying apcupsd ==="
echo "Hosts: $HOSTS"
echo ""

for HOST in $HOSTS; do
    echo "=== Deploying apcupsd to $HOST ==="

    # Create temp directory on target and copy files
    echo "Copying files to $HOST:/tmp/homelab-apcupsd/..."
    ssh "$HOST" "rm -rf /tmp/homelab-apcupsd && mkdir -p /tmp/homelab-apcupsd"
    scp -r "$SCRIPT_DIR"/* "$HOST:/tmp/homelab-apcupsd/"

    # Run installer on target
    echo "Running installer on $HOST..."
    ssh "$HOST" "cd /tmp/homelab-apcupsd && chmod +x scripts/install.sh && ./scripts/install.sh $HOST"

    # Cleanup
    ssh "$HOST" "rm -rf /tmp/homelab-apcupsd"

    echo "=== Deployment to $HOST complete ==="
    echo ""
done
