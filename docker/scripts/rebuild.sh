#!/bin/bash
# Rebuild disposable Docker/containerd runtime state, then recreate compose stacks.
# Place this file in /mnt/cache/appdata and execute it as root.

set -euo pipefail

PULL_IMAGES=true

usage() {
    cat <<'EOF'
Usage: ./rebuild.sh [--pull|--no-pull]

Manual destructive recovery for Docker/containerd runtime state on this Docker host.

This deletes disposable Docker runtime metadata and local image/layer/container state:
  /var/lib/containerd
  /var/lib/docker/{buildkit,containerd,containers,image,network,overlay2,swarm,tmp}

This preserves:
  /var/lib/docker/volumes
  bind-mounted appdata/media outside Docker runtime

After Docker starts, this joins the configured swarm only when a join token is
available, then runs start.sh to recreate compose stacks.

Swarm join token sources, in order:
  1) DOCKER_SWARM_JOIN_TOKEN environment variable
  2) /mnt/cache/appdata/.homelab/docker/swarm.token

If neither exists, swarm join is skipped.

Options:
  --pull                        Run start.sh --pull after rebuild. Default.
  --no-pull                     Run start.sh --no-pull after rebuild. Use only if images are guaranteed available.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '>>> %s\n' "$*"
}

load_docker_env() {
    local env_file="$1"

    [[ -f $env_file ]] || return 0
    # shellcheck source=/dev/null
    source "$env_file"
}

wait_for_docker() {
    local i

    for ((i = 1; i <= 120; i++)); do
        if timeout 30 docker info >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done

    return 1
}

swarm_token_file() {
    printf '%s\n' "$SCRIPT_DIR/.homelab/docker/swarm.token"
}

read_swarm_token() {
    local role="$1"
    local token_file token

    if [[ -n ${DOCKER_SWARM_JOIN_TOKEN:-} ]]; then
        printf '%s\n' "$DOCKER_SWARM_JOIN_TOKEN"
        return 0
    fi

    token_file="$(swarm_token_file)"
    if [[ -f $token_file ]]; then
        token="$(tr -d '[:space:]' < "$token_file")"
        [[ -n $token ]] || die "swarm token file is empty: $token_file"
        printf '%s\n' "$token"
        return 0
    fi

    return 1
}

join_configured_swarm() {
    [[ ${DOCKER_SWARM_ENABLED:-false} == "true" ]] || return 0

    local role token join_cmd

    [[ -n ${DOCKER_SWARM_MANAGER_ADDR:-} ]] || die "DOCKER_SWARM_MANAGER_ADDR is not configured"
    role="${DOCKER_SWARM_NODE_ROLE:-manager}"
    [[ $role == manager || $role == worker ]] || die "unsupported DOCKER_SWARM_NODE_ROLE: $role"

    if ! token="$(read_swarm_token "$role")"; then
        info "swarm is enabled, but no join token is available; skipping swarm join"
        return 0
    fi
    [[ -n $token ]] || die "failed to read swarm join token"

    join_cmd=(docker swarm join --token "$token")
    if [[ -n ${DOCKER_SWARM_ADVERTISE_ADDR:-} ]]; then
        join_cmd+=(--advertise-addr "$DOCKER_SWARM_ADVERTISE_ADDR")
    fi
    join_cmd+=("$DOCKER_SWARM_MANAGER_ADDR")

    info "joining configured swarm via $DOCKER_SWARM_MANAGER_ADDR"
    "${join_cmd[@]}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pull)
            PULL_IMAGES=true
            ;;
        --no-pull)
            PULL_IMAGES=false
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
    shift
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "run as root on the Docker host"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
START_SH="$SCRIPT_DIR/start.sh"
[[ -x $START_SH ]] || die "start.sh missing or not executable: $START_SH"
load_docker_env "$SCRIPT_DIR/.homelab/docker/env"

info "stopping Docker and containerd"
systemctl stop docker.service docker.socket containerd.service || true
sleep 3
pkill -TERM -x docker-proxy || true
pkill -TERM -x containerd-shim || true
sleep 3
pkill -KILL -x docker-proxy || true
pkill -KILL -x containerd-shim || true

info "deleting disposable Docker runtime state"
rm -rf \
    /var/lib/containerd \
    /var/lib/docker/buildkit \
    /var/lib/docker/containerd \
    /var/lib/docker/containers \
    /var/lib/docker/image \
    /var/lib/docker/network \
    /var/lib/docker/overlay2 \
    /var/lib/docker/swarm \
    /var/lib/docker/tmp

mkdir -p /var/lib/docker

info "starting containerd and Docker"
systemctl reset-failed docker.service containerd.service || true
systemctl start containerd.service
systemctl start docker.service
wait_for_docker || die "Docker did not become healthy after runtime rebuild"

join_configured_swarm

if [[ $PULL_IMAGES == true ]]; then
    info "recreating compose stacks with image pulls"
    "$START_SH" --pull
else
    info "recreating compose stacks without image pulls"
    "$START_SH" --no-pull
fi

info "container state after rebuild"
docker ps --format "table {{.Names}}\t{{.Status}}"
info "done"
