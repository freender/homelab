#!/bin/bash
# Runs `docker compose down` in each subdirectory beside this script.
# Place this file in /mnt/cache/appdata and execute it.

set -u

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

mapfile -t ORDERED_STACKS < <(get_priority_stacks "$ROOT")
mapfile -t STACK_DIRS < <(list_stack_dirs "$ROOT")
failed_stacks=()

stop_stack() {
    local stack_dir="$1"
    local stack_name="$2"
    local code

    run_compose "$stack_dir" down --remove-orphans
    code=$?
    if [[ $code -ne 0 ]]; then
        echo "!! failed in $stack_dir (exit $code)"
        failed_stacks+=("$stack_name")
        return 1
    fi

    return 0
}

# Ask for confirmation
printf "This will run 'docker compose down --remove-orphans' in all subdirectories of %s\n" "$ROOT"
printf "Are you sure you want to continue? (yes/no): "
read -r response

case "$response" in
  yes|YES|y|Y)
    echo "Proceeding..."
    ;;
  *)
    echo "Aborted."
    exit 0
    ;;
esac

if ! acquire_docker_lock "$ROOT" "rm"; then
    exit 1
fi

found=0
for d in "${STACK_DIRS[@]}"; do
    [[ -d "$d" ]] || continue
    stack_name="$(basename "$d")"
    if stack_in_list "$stack_name" "${ORDERED_STACKS[@]}"; then
        continue
    fi

    found=1
    echo ">>> $d"
    stop_stack "$d" "$stack_name" || true
done

for (( idx=${#ORDERED_STACKS[@]} - 1; idx>=0; idx-- )); do
    d="$ROOT/${ORDERED_STACKS[$idx]}"
    [[ -d "$d" ]] || continue

    found=1
    echo ">>> $d (priority order)"
    stop_stack "$d" "${ORDERED_STACKS[$idx]}" || true
done

[ "$found" -eq 0 ] && echo "No compose stacks found under $ROOT"
echo "Done."

if [[ ${#failed_stacks[@]} -gt 0 ]]; then
    print_failed_stacks "stop" "${failed_stacks[@]}"
    exit 1
fi
