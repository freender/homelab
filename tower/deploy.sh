#!/bin/bash
# Deploy tower-specific scripts
# Usage: ./deploy.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Supported hosts for this module
SUPPORTED_HOSTS=("tower")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping tower (not applicable to $1)"
        exit 0
    fi
fi
HOST="tower"
DEST_DIR="/mnt/cache/appdata/scripts"

SCRIPTS=(
    "filebot_monitor.sh"
)

echo "==> Deploying Tower Scripts"
echo "    Repository: https://github.com/freender/homelab"
echo "    Destination: ${DEST_DIR}"
echo ""

echo "==> Deploying to ${HOST}..."

echo "    Copying scripts..."
for script in "${SCRIPTS[@]}"; do
    scp "${SCRIPT_DIR}/${script}" "${HOST}:/tmp/${script}"
    ssh "$HOST" "sudo mv /tmp/${script} ${DEST_DIR}/${script} && sudo chmod +x ${DEST_DIR}/${script}"
    echo "      - ${script}"
done

echo ""
echo "    ✓ Deployment complete"
echo ""
echo "Scheduling handled by User Scripts plugin on tower."
echo "Update User Scripts wrappers to point to:"
echo "  /mnt/cache/appdata/scripts/filebot_monitor.sh"
