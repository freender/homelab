#!/bin/bash
# install.sh - Install Proxmox post-install webhook deploy trigger.

set -euo pipefail

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

print_header "PVE Post-Install Webhook"

missing_pkgs=()
command -v git >/dev/null 2>&1 || missing_pkgs+=(git)
command -v ssh >/dev/null 2>&1 || missing_pkgs+=(openssh-client)
command -v flock >/dev/null 2>&1 || missing_pkgs+=(util-linux)
python3 - <<'PY' >/dev/null 2>&1 || missing_pkgs+=(python3-yaml)
import yaml
PY

if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    print_sub "Installing packages: ${missing_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing_pkgs[@]}"
fi

mkdir -p /etc/homelab-postinstall-webhook \
         /var/lib/homelab-postinstall-webhook/events \
         /var/lib/homelab-postinstall-webhook/state
chmod 700 /etc/homelab-postinstall-webhook /var/lib/homelab-postinstall-webhook/events

ic() { local rc=0; install_if_changed "$@" || rc=$?; [[ $rc -le 1 ]] || return "$rc"; }

print_action "Installing webhook scripts"
ic "$SCRIPT_DIR/scripts/homelab-postinstall-webhook.py" /usr/local/sbin/homelab-postinstall-webhook 755 homelab-postinstall-webhook
ic "$SCRIPT_DIR/scripts/homelab-pdm-installation-watch.py" /usr/local/sbin/homelab-pdm-installation-watch 755 homelab-pdm-installation-watch
ic "$SCRIPT_DIR/scripts/homelab-pdm-refresh-remote.py" /usr/local/sbin/homelab-pdm-refresh-remote 755 homelab-pdm-refresh-remote
ic "$SCRIPT_DIR/scripts/homelab-postinstall-deploy.sh" /usr/local/sbin/homelab-postinstall-deploy 755 homelab-postinstall-deploy

print_action "Installing root SSH agent + 1Password key loader"
install -d -m 700 /root/.local/bin /root/.config
ic "$SCRIPT_DIR/scripts/op-ssh-add" /root/.local/bin/op-ssh-add 700 op-ssh-add
ic "$SCRIPT_DIR/scripts/addhomelabkeys" /root/.local/bin/addhomelabkeys 700 addhomelabkeys
ic "$SCRIPT_DIR/scripts/op-ssh-agent.conf" /root/.config/op-ssh-agent.env 600 op-ssh-agent.env

print_action "Installing webhook config"
install -m 0600 "$BUILD_DIR/env" /etc/homelab-postinstall-webhook/env

print_action "Installing systemd service"
ic "$SCRIPT_DIR/scripts/homelab-postinstall-webhook.service" \
   /etc/systemd/system/homelab-postinstall-webhook.service 644 homelab-postinstall-webhook.service
ic "$SCRIPT_DIR/scripts/homelab-pdm-installation-watch.service" \
   /etc/systemd/system/homelab-pdm-installation-watch.service 644 homelab-pdm-installation-watch.service
ic "$SCRIPT_DIR/scripts/homelab-pdm-installation-watch.timer" \
   /etc/systemd/system/homelab-pdm-installation-watch.timer 644 homelab-pdm-installation-watch.timer
ic "$SCRIPT_DIR/scripts/homelab-ssh-agent.service" \
   /etc/systemd/system/homelab-ssh-agent.service 644 homelab-ssh-agent.service
ic "$SCRIPT_DIR/scripts/homelab-op-ssh-load.service" \
   /etc/systemd/system/homelab-op-ssh-load.service 644 homelab-op-ssh-load.service
ic "$SCRIPT_DIR/scripts/homelab-op-ssh-load.timer" \
   /etc/systemd/system/homelab-op-ssh-load.timer 644 homelab-op-ssh-load.timer

systemctl daemon-reload
systemctl enable --now homelab-postinstall-webhook.service
systemctl restart homelab-postinstall-webhook.service
systemctl enable --now homelab-pdm-installation-watch.timer
systemctl enable --now homelab-ssh-agent.service
systemctl enable --now homelab-op-ssh-load.timer
systemctl start homelab-op-ssh-load.service

print_ok "post-install webhook listener installed"
print_sub "Status: systemctl status homelab-postinstall-webhook.service --no-pager"
print_sub "PDM watch: systemctl list-timers homelab-pdm-installation-watch.timer --no-pager"
print_sub "SSH agent: systemctl status homelab-ssh-agent.service --no-pager"
print_sub "SSH key load: systemctl list-timers homelab-op-ssh-load.timer --no-pager"
