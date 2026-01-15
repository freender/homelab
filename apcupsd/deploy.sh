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

# Validate telegram.env exists locally before deployment
if [[ ! -f "$SCRIPT_DIR/shared/telegram/telegram.env" ]]; then
    echo "ERROR: telegram.env not found!"
    echo ""
    echo "Create it from the example:"
    echo "  cp $SCRIPT_DIR/shared/telegram/telegram.env.example $SCRIPT_DIR/shared/telegram/telegram.env"
    echo "  # Edit with your actual TELEGRAM_TOKEN and TELEGRAM_CHATID"
    echo ""
    echo "NOTE: telegram.env is gitignored and should never be committed."
    exit 1
fi

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
