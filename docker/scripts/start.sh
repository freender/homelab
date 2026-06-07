#!/bin/bash
# Runs `docker compose up -d` in each subdirectory beside this script.
# Place this file in /mnt/cache/appdata and execute it.
# Supports custom startup order for dependencies.
# Pulls images by default; use `--no-pull` for fast restart-only runs.
# Pruning is opt-in; use `--prune` after confirming no shared resources are at risk.

set -u

PULL_IMAGES=true
PRUNE_IMAGES=false
PRUNE_VOLUMES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pull)
            PULL_IMAGES=true
            ;;
        --no-pull)
            PULL_IMAGES=false
            ;;
        --prune)
            PRUNE_IMAGES=true
            ;;
        --prune-volumes)
            PRUNE_IMAGES=true
            PRUNE_VOLUMES=true
            ;;
        *)
            echo "Usage: $0 [--pull|--no-pull] [--prune] [--prune-volumes]" >&2
            exit 1
            ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMON_SH=""

if [[ -f "$SCRIPT_DIR/.homelab/docker/docker-common.sh" ]]; then
    COMMON_SH="$SCRIPT_DIR/.homelab/docker/docker-common.sh"
elif [[ -f "$SCRIPT_DIR/scripts/docker-common.sh" ]]; then
    COMMON_SH="$SCRIPT_DIR/scripts/docker-common.sh"
elif [[ -f "$SCRIPT_DIR/docker-common.sh" ]]; then
    COMMON_SH="$SCRIPT_DIR/docker-common.sh"
else
    echo "!! docker-common.sh not found" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$COMMON_SH"

# Base directory = directory of deployed script location
ROOT="$(resolve_appdata_root "$0")"

if ! acquire_docker_lock "$ROOT" "start"; then
    exit 1
fi

start_stack() {
    local stack_dir="$1"
    local stack_name="$2"
    local code

    if [[ "$PULL_IMAGES" == "true" ]]; then
        run_compose "$stack_dir" pull
        code=$?
        if [[ $code -ne 0 ]]; then
            echo "!! failed in $stack_dir during pull (exit $code)"
            failed_stacks+=("$stack_name")
            return 1
        fi
    fi

    run_compose "$stack_dir" up -d
    code=$?
    if [[ $code -ne 0 ]]; then
        echo "!! failed in $stack_dir during up (exit $code)"
        failed_stacks+=("$stack_name")
        return 1
    fi

    return 0
}

# Define startup order (stacks that need to run first)
mapfile -t ORDERED_STACKS < <(get_priority_stacks "$ROOT")
mapfile -t STACK_DIRS < <(list_stack_dirs "$ROOT")

# Track which stacks we've already started
declare -A started_stacks
failed_stacks=()
prune_failed=false

echo "=== Starting Docker stacks with custom order ==="
if [[ "$PULL_IMAGES" == "true" ]]; then
    echo "Mode: pull images before startup"
else
    echo "Mode: skip image pulls"
fi
echo ""

# Start ordered stacks first
for stack in "${ORDERED_STACKS[@]}"; do
    stack_dir="$ROOT/$stack"
    if [[ ! -d "$stack_dir" ]]; then
        echo "!! WARNING: Ordered stack '$stack' not found at $stack_dir"
        continue
    fi

    echo ">>> $stack_dir (priority order)"
    start_stack "$stack_dir" "$stack" || true
    started_stacks["$stack"]=1
done

[[ ${#ORDERED_STACKS[@]} -gt 0 ]] && echo "" && echo ">>> Starting remaining stacks..." && echo ""

# Now start all other stacks
found=0
for d in "${STACK_DIRS[@]}"; do
    [[ -d "$d" ]] || continue

    stack_name="$(basename "$d")"
    [[ "${started_stacks[$stack_name]:-0}" == "1" ]] && continue

    found=1
    echo ">>> $d"
    start_stack "$d" "$stack_name" || true
done

echo ""
[[ "$found" -eq 0 && ${#ORDERED_STACKS[@]} -eq 0 ]] && echo "No compose stacks found under $ROOT"

echo ""
if [[ "$PRUNE_IMAGES" == "true" ]]; then
    if [[ "$PRUNE_VOLUMES" == "true" ]]; then
        echo ">>> Pruning unused Docker images and volumes"
        docker system prune -f --volumes
    else
        echo ">>> Pruning unused Docker images"
        docker image prune -af
    fi
    code=$?
    if [[ $code -ne 0 ]]; then
        echo "!! docker prune failed (exit $code)"
        prune_failed=true
    fi
else
    echo ">>> Skipping Docker prune (use --prune to opt in)"
fi

echo ""
echo "=== Done ==="

if [[ ${#failed_stacks[@]} -gt 0 ]]; then
    print_failed_stacks "start" "${failed_stacks[@]}"
    exit 1
fi

[[ "$prune_failed" == "true" ]] && exit 1
exit 0
