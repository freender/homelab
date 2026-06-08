#!/bin/bash
set -euo pipefail

HOST=${1:?host required}
EVENT_FILE=${2:-}
CONFIG_FILE=/etc/homelab-postinstall-webhook/env

if [[ -r "$CONFIG_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
fi

REPO_DIR=${REPO_DIR:-/root/homelab}
DRY_RUN=${DRY_RUN:-true}
SSH_TIMEOUT_SECONDS=${SSH_TIMEOUT_SECONDS:-1200}
DEPLOY_TIMEOUT_SECONDS=${DEPLOY_TIMEOUT_SECONDS:-3600}
SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-/root/.ssh/agent.sock}
HOME=${HOME:-/root}
export SSH_AUTH_SOCK
export HOME

log() {
    printf 'homelab-postinstall-deploy[%s]: %s\n' "$HOST" "$*"
}

python_bin() {
    if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
        printf '%s\n' "$REPO_DIR/.venv/bin/python"
    else
        printf 'python3\n'
    fi
}

read_host_info() {
    local py
    py=$(python_bin)
    "$py" - "$REPO_DIR" "$HOST" <<'PY'
import sys
from pathlib import Path

import yaml

repo = Path(sys.argv[1])
host = sys.argv[2]
data = yaml.safe_load((repo / "hosts.conf").read_text()) or {}
cfg = data[host]["config"]
print(cfg["type"])
print(cfg.get("hostname", host))
print(cfg.get("user", "root"))
print("true" if cfg.get("type") == "pve" and cfg.get("standalone") is True else "false")
PY
}

mapfile -t host_info < <(read_host_info)
host_type=${host_info[0]}
ssh_hostname=${host_info[1]}
ssh_user=${host_info[2]}
standalone_pve=${host_info[3]}

ssh_opts=(
    -o BatchMode=yes
    -o ConnectTimeout=5
    -o StrictHostKeyChecking=accept-new
    -o UserKnownHostsFile=/root/.ssh/known_hosts
)

ready_command='true'
if [[ "$host_type" == "pve" ]]; then
    ready_command='systemctl is-active --quiet pve-cluster && pvesh get /version >/dev/null'
fi

wait_for_ready() {
    log "waiting for SSH readiness on $ssh_user@$ssh_hostname (type=$host_type)"
    ssh-keygen -R "$ssh_hostname" >/dev/null 2>&1 || true
    ssh-keygen -R "$HOST" >/dev/null 2>&1 || true

    local deadline ready
    deadline=$(( $(date +%s) + SSH_TIMEOUT_SECONDS ))
    ready=false
    while [[ $(date +%s) -lt $deadline ]]; do
        if ssh "${ssh_opts[@]}" "$ssh_user@$ssh_hostname" "$ready_command" >/dev/null 2>&1; then
            ready=true
            break
        fi
        sleep 10
    done

    if [[ "$ready" != true ]]; then
        log "ERROR: $HOST did not become ready within ${SSH_TIMEOUT_SECONDS}s"
        exit 1
    fi

    log "$HOST is ready"
}

run_deploy() {
    local module=$1
    local -a args

    args=()
    if [[ "$DRY_RUN" == "true" ]]; then
        args+=(--dry-run)
        log "running dry-run deploy: $module $HOST"
    else
        log "running deploy: $module $HOST"
    fi

    timeout "$DEPLOY_TIMEOUT_SECONDS" ./deploy "${args[@]}" "$module" "$HOST"
}

refresh_pdm_remote() {
    if [[ "$DRY_RUN" == "true" ]]; then
        log "dry-run: skipping PDM remote refresh"
        return
    fi
    if [[ "$host_type" != "pve" || "$standalone_pve" != "true" ]]; then
        return
    fi

    log "refreshing PDM remote trust/token for $HOST"
    if /usr/local/sbin/homelab-pdm-refresh-remote "$HOST"; then
        log "PDM remote refresh complete"
    else
        log "WARNING: PDM remote refresh failed; continuing post-install deploy"
    fi
}

reboot_host() {
    local reason=$1

    if [[ "$DRY_RUN" == "true" ]]; then
        log "dry-run: skipping reboot ($reason)"
        return
    fi

    log "rebooting $HOST ($reason)"
    ssh "${ssh_opts[@]}" "$ssh_user@$ssh_hostname" 'systemctl reboot' >/dev/null 2>&1 || true
    sleep 20
}

run_default_deploy() {
    if [[ "$host_type" == "pve" && "$standalone_pve" != "true" ]]; then
        log "cluster host detected; post-reinstall delete/rejoin and second deploy remain manual"
    fi
    run_deploy all
}

run_standalone_pve_gated_deploy() {
    log "running gated standalone PVE post-install deploy: pass 1"
    run_deploy all
    reboot_host "apply first-pass post-install changes"
    wait_for_ready

    log "running gated standalone PVE post-install deploy: pass 2"
    run_deploy all
}

log "event=$EVENT_FILE"
wait_for_ready
log "starting deploy"
(
    flock -x 9
    cd "$REPO_DIR"
    git pull --ff-only
    refresh_pdm_remote
    if [[ "$standalone_pve" == "true" ]]; then
        run_standalone_pve_gated_deploy
    else
        run_default_deploy
    fi
) 9>/run/homelab-postinstall-deploy.lock

log "deploy finished"
