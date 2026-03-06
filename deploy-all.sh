#!/bin/bash
# Deploy homelab modules
# Usage: ./deploy-all.sh [--dry-run] [--force] [hostname|all]
#   ./deploy-all.sh                 - Deploy all modules to all hosts
#   ./deploy-all.sh tower           - Deploy applicable modules to tower only
#   ./deploy-all.sh --dry-run all   - Preview deployments
#   ./deploy-all.sh --force all     - Force installers to rewrite managed files

set -u

# Parse flags
DRY_RUN=false
FORCE_UPDATE=false
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n)
            DRY_RUN=true
            export DRY_RUN
            shift
            ;;
        --force|--force-update)
            FORCE_UPDATE=true
            export FORCE_UPDATE
            shift
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

HOST="${ARGS[0]:-all}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODULES=()
while IFS= read -r -d '' module_dir; do
    module="$(basename "$module_dir")"
    if [[ -f "${module_dir}/deploy.sh" ]]; then
        MODULES+=("$module")
    fi
done < <(find "$SCRIPT_DIR" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

PREFERRED_ORDER=(
    pve-postinstall
    apcupsd
    apcupsd-exporter
    pve-exporters
    telegraf
    pve-gpu-passthrough
    ssh
    docker
    apt-upgrade
)

ORDERED_MODULES=()
for preferred in "${PREFERRED_ORDER[@]}"; do
    for module in "${MODULES[@]}"; do
        if [[ "$module" == "$preferred" ]]; then
            ORDERED_MODULES+=("$module")
            break
        fi
    done
done

for module in "${MODULES[@]}"; do
    found=false
    for ordered in "${ORDERED_MODULES[@]}"; do
        if [[ "$module" == "$ordered" ]]; then
            found=true
            break
        fi
    done
    if [[ "$found" == false ]]; then
        ORDERED_MODULES+=("$module")
    fi
done

MODULES=("${ORDERED_MODULES[@]}")

if [[ ${#MODULES[@]} -eq 0 ]]; then
    echo "No deployable modules found in ${SCRIPT_DIR}"
    exit 1
fi

FAILED_MODULES=()

echo "==> Deploying homelab to: $HOST"
echo ""

for module in "${MODULES[@]}"; do
    script="${SCRIPT_DIR}/${module}/deploy.sh"
    
    if [[ ! -x "$script" ]]; then
        echo "==> Skipping $module (missing deploy script)"
        FAILED_MODULES+=("$module")
        continue
    fi
    
    # Run module deploy, capture exit code
    if [[ "$DRY_RUN" == "true" && "$FORCE_UPDATE" == "true" ]]; then
        "$script" --dry-run --force "$HOST"
    elif [[ "$DRY_RUN" == "true" ]]; then
        "$script" --dry-run "$HOST"
    elif [[ "$FORCE_UPDATE" == "true" ]]; then
        "$script" --force "$HOST"
    else
        "$script" "$HOST"
    fi
    exit_code=$?
    
    # exit 0 = success or skipped (handled by module)
    # exit non-zero = failure
    if [[ $exit_code -ne 0 ]]; then
        FAILED_MODULES+=("$module")
    fi
done

echo ""
echo "==> Deploy complete!"

if [[ ${#FAILED_MODULES[@]} -gt 0 ]]; then
    echo "Failed modules: ${FAILED_MODULES[*]}"
    exit 1
fi
