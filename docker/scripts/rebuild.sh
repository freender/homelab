#!/bin/bash
# Rebuild disposable Docker/containerd runtime state, then recreate compose stacks.
# Place this file in /mnt/cache/appdata and execute it as root.

set -euo pipefail

PULL_IMAGES=true
CONFIRM=false

usage() {
    cat <<'EOF'
Usage: ./rebuild.sh --yes [--pull|--no-pull]

Manual destructive recovery for Docker/containerd runtime state on this Docker host.

This deletes disposable Docker runtime metadata and local image/layer/container state:
  /var/lib/containerd
  /var/lib/docker/{buildkit,containerd,containers,image,network,overlay2,tmp}

This preserves:
  /var/lib/docker/swarm, backed up and restored
  /var/lib/docker/volumes
  bind-mounted appdata/media outside Docker runtime

After Docker starts, this runs start.sh to recreate compose stacks.

Options:
  --yes                         Required confirmation.
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

docker_identity() {
    docker info --format '{{.Swarm.LocalNodeState}} {{.Swarm.NodeID}}' 2>/dev/null || printf 'unknown -\n'
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)
            CONFIRM=true
            ;;
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
[[ $CONFIRM == true ]] || die "--yes is required"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
START_SH="$SCRIPT_DIR/start.sh"
[[ -x $START_SH ]] || die "start.sh missing or not executable: $START_SH"

read -r BEFORE_SWARM_STATE BEFORE_SWARM_NODE < <(docker_identity)
info "swarm identity before rebuild: state=$BEFORE_SWARM_STATE node=$BEFORE_SWARM_NODE"

BACKUP_DIR="/root/homelab-docker-runtime-backup/$(date +%Y%m%d-%H%M%S)"
info "stopping Docker and containerd"
systemctl stop docker.service docker.socket containerd.service || true
sleep 3
pkill -TERM -x docker-proxy || true
pkill -TERM -x containerd-shim || true
sleep 3
pkill -KILL -x docker-proxy || true
pkill -KILL -x containerd-shim || true

info "backing up swarm identity to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
if [[ -d /var/lib/docker/swarm ]]; then
    cp -a /var/lib/docker/swarm "$BACKUP_DIR/swarm"
fi

info "deleting disposable Docker runtime state"
rm -rf \
    /var/lib/containerd \
    /var/lib/docker/buildkit \
    /var/lib/docker/containerd \
    /var/lib/docker/containers \
    /var/lib/docker/image \
    /var/lib/docker/network \
    /var/lib/docker/overlay2 \
    /var/lib/docker/tmp

mkdir -p /var/lib/docker
if [[ ! -d /var/lib/docker/swarm && -d "$BACKUP_DIR/swarm" ]]; then
    cp -a "$BACKUP_DIR/swarm" /var/lib/docker/
fi

info "starting containerd and Docker"
systemctl reset-failed docker.service containerd.service || true
systemctl start containerd.service
systemctl start docker.service
wait_for_docker || die "Docker did not become healthy after runtime rebuild"

read -r AFTER_SWARM_STATE AFTER_SWARM_NODE < <(docker_identity)
if [[ $BEFORE_SWARM_STATE == active ]]; then
    [[ $AFTER_SWARM_STATE == active ]] || die "swarm was active before rebuild but is now $AFTER_SWARM_STATE"
    [[ $AFTER_SWARM_NODE == "$BEFORE_SWARM_NODE" ]] || die "swarm node ID changed: before=$BEFORE_SWARM_NODE after=$AFTER_SWARM_NODE"
fi
info "swarm identity after rebuild: state=$AFTER_SWARM_STATE node=$AFTER_SWARM_NODE"

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
