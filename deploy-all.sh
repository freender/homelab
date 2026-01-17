#!/bin/bash
# Deploy all homelab modules
# Usage: ./deploy-all.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODULES=(
    "ssh"
    "apcupsd"
    "telegraf"
    "zfs"
    "docker"
    "tower"
    "pve-interfaces"
    "pve-gpu-passthrough"
)

declare -A MODULE_SCRIPTS=(
    ["ssh"]="${SCRIPT_DIR}/ssh/deploy.sh"
    ["apcupsd"]="${SCRIPT_DIR}/apcupsd/deploy.sh"
    ["telegraf"]="${SCRIPT_DIR}/telegraf/deploy.sh"
    ["zfs"]="${SCRIPT_DIR}/zfs/deploy.sh"
    ["docker"]="${SCRIPT_DIR}/docker/deploy.sh"
    ["tower"]="${SCRIPT_DIR}/tower/deploy.sh"
    ["pve-interfaces"]="${SCRIPT_DIR}/pve-interfaces/deploy.sh"
    ["pve-gpu-passthrough"]="${SCRIPT_DIR}/pve-gpu-passthrough/deploy.sh"
)

FAILED_MODULES=()

run_module() {
    local module="$1"
    local script="${MODULE_SCRIPTS[$module]}"

    echo "==> Deploying module: ${module}"

    if [[ ! -x "$script" ]]; then
        echo "    ✗ Missing deploy script: ${script}"
        FAILED_MODULES+=("
${module}")
        echo ""
        return
    fi

    if "$script" all; then
        echo "    ✓ ${module} deployment complete"
    else
        echo "    ✗ ${module} deployment failed"
        FAILED_MODULES+=("
${module}")
    fi

    echo ""
}

echo "==> Deploying all homelab modules"
echo ""

for module in "${MODULES[@]}"; do
    run_module "$module"
done

echo "==> Deploy all complete!"

if [[ ${#FAILED_MODULES[@]} -gt 0 ]]; then
    echo "Failed modules: ${FAILED_MODULES[*]}"
    exit 1
fi
