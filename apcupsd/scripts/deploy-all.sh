#!/bin/bash
# deploy-all.sh - Deploy apcupsd to all hosts
# Order: xur (isolated) -> ace, clovis (slaves) -> bray (master)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying apcupsd to all hosts ==="
echo "Order: xur -> ace -> clovis -> bray"
echo ""

# Deploy in safe order
for HOST in xur ace clovis bray; do
  echo "----------------------------------------"
  "$SCRIPT_DIR/deploy.sh" $HOST
  echo ""
done

echo "=== All deployments complete ==="
echo ""
echo "Verification commands:"
echo "  ssh bray "apcaccess status | grep STATUS""
echo "  ssh ace "apcaccess status 10.0.40.40:3551 | grep STATUS""
echo "  ssh clovis "apcaccess status 10.0.40.40:3551 | grep STATUS""
echo "  ssh xur "apcaccess status | grep STATUS""
