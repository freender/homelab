#!/bin/bash
# deploy.sh - Deploy apcupsd config to remote host via SSH
# Usage: ./deploy.sh <hostname>

set -e

HOST=$1
[[ -z "$HOST" ]] && { echo "Usage: $0 <hostname>"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Deploying apcupsd to $HOST ==="

# Create temp directory on target and copy files
echo "Copying files to $HOST:/tmp/homelab-apcupsd/..."
ssh $HOST "rm -rf /tmp/homelab-apcupsd && mkdir -p /tmp/homelab-apcupsd"
scp -r "$SCRIPT_DIR"/* "$HOST:/tmp/homelab-apcupsd/"

# Run installer on target
echo "Running installer on $HOST..."
ssh $HOST "cd /tmp/homelab-apcupsd && chmod +x install.sh && ./install.sh $HOST"

# Cleanup
ssh $HOST "rm -rf /tmp/homelab-apcupsd"

echo ""
echo "=== Deployment to $HOST complete ==="
