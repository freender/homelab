#!/bin/bash

has_compose_file() {
    local dir="$1"

    [[ -f "$dir/compose.yml" || -f "$dir/docker-compose.yml" ]]
}

resolve_appdata_root() {
    local script_path="${1:-$0}"
    local script_dir

    script_dir="$(cd "$(dirname "$script_path")" && pwd)"

    if [[ -f "$script_dir/.homelab/docker/docker-common.sh" ]]; then
        printf '%s\n' "$script_dir"
    elif [[ -f "$script_dir/scripts/docker-common.sh" ]]; then
        printf '%s\n' "$script_dir"
    elif [[ -f "$script_dir/docker-common.sh" ]]; then
        if [[ "$(basename "$script_dir")" == "scripts" ]]; then
            printf '%s\n' "$(dirname "$script_dir")"
        else
            printf '%s\n' "$script_dir"
        fi
    else
        printf '%s\n' "$script_dir"
    fi
}

list_stack_dirs() {
    local root="$1"
    local dir

    for dir in "$root"/*/; do
        [[ -d "$dir" ]] || continue
        if has_compose_file "$dir"; then
            printf '%s\n' "${dir%/}"
        fi
    done
}

get_priority_stacks() {
    local root="$1"
    local stack

    for stack in traefik traefik-helm traefik-neo; do
        if has_compose_file "$root/$stack"; then
            printf '%s\n' "$stack"
        fi
    done
}

stack_in_list() {
    local needle="$1"
    shift

    local item
    for item in "$@"; do
        [[ "$item" == "$needle" ]] && return 0
    done

    return 1
}

run_compose() {
    local stack_dir="$1"
    shift

    (
        cd "$stack_dir" && docker compose "$@"
    )
}

resolve_docker_lock_file() {
    local root="$1"
    local runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
    local lock_dir="$runtime_dir/homelab/docker"
    local root_hash

    root_hash="$(cksum <<<"$root")"
    root_hash="${root_hash%% *}"

    mkdir -p "$lock_dir" || return 1
    printf '%s\n' "$lock_dir/docker-stacks-${root_hash}.lock"
}

acquire_docker_lock() {
    local root="$1"
    local operation="$2"
    local lock_file
    local lock_fd

    if [[ "${DOCKER_STACKS_LOCK_HELD:-0}" == "1" ]]; then
        return 0
    fi

    lock_file="$(resolve_docker_lock_file "$root")" || {
        echo "!! Could not prepare Docker lock directory for $operation" >&2
        return 1
    }

    if ! exec {lock_fd}> "$lock_file"; then
        echo "!! Could not open Docker lock file $lock_file for $operation" >&2
        return 1
    fi

    if ! flock -n "$lock_fd"; then
        echo "!! Another Docker stack operation is already running; cannot start $operation"
        return 1
    fi

    export DOCKER_STACKS_LOCK_FD="$lock_fd"
    export DOCKER_STACKS_LOCK_HELD=1
    return 0
}

print_failed_stacks() {
    local action="$1"
    shift

    local stack
    echo ""
    echo "!! Failed to $action the following stack(s):"
    for stack in "$@"; do
        echo "   - $stack"
    done
}
