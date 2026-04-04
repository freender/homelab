#!/bin/bash
# rebuild.sh - Post OS-reinstall setup for cinci/cottonwood
# Assumes: ZFS data disk already imported (zpool import -f {{ ZFS_POOL }})
# Usage: sudo bash {{ ZFS_MOUNTPOINT }}/appdata/scripts/rebuild.sh

set -e

ZFS_POOL="{{ ZFS_POOL }}"
ZFS_MOUNTPOINT="{{ ZFS_MOUNTPOINT }}"
SYSTEM_HOSTNAME="{{ SYSTEM_HOSTNAME }}"
SYSTEM_TIMEZONE="{{ SYSTEM_TIMEZONE }}"
APPDATA="${ZFS_MOUNTPOINT}/appdata"
SCRIPTS_DIR="${APPDATA}/scripts"
WG_BACKUP="${ZFS_MOUNTPOINT}/.system/wireguard/wg0.conf"
DOCKER_INSTALL_SH="${SCRIPTS_DIR}/docker-install.sh"
SANOID_CONF_BACKUP="${SCRIPTS_DIR}/sanoid.conf"

info() { echo "==> $*"; }
ok() { echo "    ✓ $*"; }
warn() { echo "    ! $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

info "Preflight checks"

if [[ "$(id -u)" -ne 0 ]]; then
    die "Run as root: sudo bash $0"
fi

if ! mountpoint -q "$ZFS_MOUNTPOINT"; then
    die "$ZFS_MOUNTPOINT is not mounted. Run 'zpool import -f ${ZFS_POOL}' first."
fi

if [[ ! -f "$APPDATA/start.sh" ]]; then
    die "$APPDATA/start.sh not found. ZFS data looks incomplete."
fi

if [[ ! -f "$DOCKER_INSTALL_SH" ]]; then
    die "$DOCKER_INSTALL_SH not found. Re-deploy ubuntu-setup once the box is back online."
fi

ok "ZFS data disk mounted"

info "Hostname and timezone"
hostnamectl set-hostname "$SYSTEM_HOSTNAME"
timedatectl set-timezone "$SYSTEM_TIMEZONE"
ok "Configured hostname=$SYSTEM_HOSTNAME timezone=$SYSTEM_TIMEZONE"

# shellcheck source=/dev/null
source "$DOCKER_INSTALL_SH"

info "Docker CE"
ensure_docker_installed

REAL_USER="${SUDO_USER:-}"
if [[ -n "$REAL_USER" ]] && ! id -nG "$REAL_USER" | grep -qw docker; then
    usermod -aG docker "$REAL_USER"
    ok "Added $REAL_USER to docker group"
fi

if [[ -f "$SANOID_CONF_BACKUP" ]]; then
    info "Sanoid / Syncoid"
    apt-get install -y -q sanoid
    mkdir -p /etc/sanoid
    cp "$SANOID_CONF_BACKUP" /etc/sanoid/sanoid.conf
    chmod 644 /etc/sanoid/sanoid.conf
    systemctl disable --now sanoid.timer >/dev/null 2>&1 || true
    ok "Sanoid config restored"
fi

info "WireGuard"
if [[ -f "$WG_BACKUP" ]]; then
    apt-get install -y -q wireguard
    echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-wireguard.conf
    sysctl --system >/dev/null

    mkdir -p /etc/wireguard
    cp "$WG_BACKUP" /etc/wireguard/wg0.conf
    chmod 600 /etc/wireguard/wg0.conf
    systemctl enable --now wg-quick@wg0
    ok "WireGuard enabled (wg0)"
else
    warn "No WireGuard backup at $WG_BACKUP - skipping"
fi

info "Systemd timers"
UNITS=()
unit_count=0
for f in "$SCRIPTS_DIR"/*.service "$SCRIPTS_DIR"/*.timer; do
    [[ -f "$f" ]] || continue
    cp "$f" /etc/systemd/system/
    UNITS+=("$(basename "$f")")
    unit_count=$((unit_count + 1))
    echo "    Installed: $(basename "$f")"
done

if (( unit_count == 0 )); then
    warn "No .service/.timer files found in $SCRIPTS_DIR - skipping"
else
    systemctl daemon-reload
    for unit in "${UNITS[@]}"; do
        if [[ "$unit" == *.timer ]]; then
            systemctl enable --now "$unit"
            ok "Enabled: $unit"
        fi
    done
fi

info "Starting Docker stacks"
bash "$APPDATA/start.sh"

echo ""
echo "=== Rebuild complete ==="
echo ""
echo "Active timers:"
systemctl list-timers --no-pager | grep homelab || true
