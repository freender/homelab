#!/bin/bash
# Deploy homelab modules
# Usage: ./deploy-all.sh [hostname|all]
#   ./deploy-all.sh          - Deploy all modules to all hosts
#   ./deploy-all.sh tower    - Deploy applicable modules to tower only
#   ./deploy-all.sh helm     - Deploy applicable modules to helm only

set -u

HOST="${1:-all}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODULES=(
    "ssh"
    "apcupsd"
    "telegraf"
    "zfs"
    "docker"
    "filebot"
    "pve-interfaces"
    "pve-gpu-passthrough"
)

FAILED_MODULES=()

echo "==> Deploying homelab to: $HOST"
echo ""

for module in "${MODULES[@]}"; do
    script="${SCRIPT_DIR}/${module}/deploy.sh"
    
    if [[ ! -x "$script" ]]; then
        echo "==> Skipping $module (missing deploy script)"
        FAILED_MODULES+=("
$module")
        continue
    fi
    
    # Run module deploy, capture exit code
    "$script" "$HOST"
    exit_code=$?
    
    # exit 0 = success or skipped (handled by module)
    # exit non-zero = failure
    if [[ $exit_code -ne 0 ]]; then
        FAILED_MODULES+=("
$module")
    fi
done

echo ""
echo "==> Deploy complete!"

if [[ ${#FAILED_MODULES[@]} -gt 0 ]]; then
    echo "Failed modules: ${FAILED_MODULES[*]}"
    exit 1
fi
