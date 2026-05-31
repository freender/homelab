#!/bin/bash
# install.sh - Deploy PXE service configs to saint (CT 112 on osiris).
# Usage: ./scripts/install.sh [hostname]
# Installs: dnsmasq proxyPXE config, nginx vhost, iPXE menu files, operational
#           scripts (pxe-enable, pxe-disable, pxe-autoupdate), and the
#           pxe-autoupdate systemd service + timer.
# Does NOT stage the Proxmox ISO or initrd — those are managed at runtime by pxe-autoupdate.

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

print_header "PVE PXE (saint)"

# ── Packages ────────────────────────────────────────────────────────────────
missing_pkgs=()
command -v nginx    >/dev/null 2>&1 || missing_pkgs+=(nginx)
command -v dnsmasq  >/dev/null 2>&1 || missing_pkgs+=(dnsmasq)
command -v rsync    >/dev/null 2>&1 || missing_pkgs+=(rsync)
command -v flock    >/dev/null 2>&1 || missing_pkgs+=(util-linux)
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    print_sub "Installing packages: ${missing_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing_pkgs[@]}"
else
    print_sub "All required packages already installed"
fi

# ── Directory layout ─────────────────────────────────────────────────────────
mkdir -p /srv/pxe /srv/tftp /etc/saint \
         /var/lib/node_exporter/textfile \
         /etc/nginx/sites-available /etc/nginx/sites-enabled \
         /etc/dnsmasq.d /root/iso

# install_if_changed returns 0=updated, 1=unchanged, 2=error.
# Guard each call so set -e does not treat "unchanged" (rc=1) as failure.
ic() { local rc=0; install_if_changed "$@" || rc=$?; [[ $rc -le 1 ]] || return "$rc"; }

# ── Management config (PDM URL + fingerprint) ────────────────────────────────
print_action "Installing management config"
ic "$BUILD_DIR/pxe-mgmt.conf" /etc/saint/pxe-mgmt.conf 600 /etc/saint/pxe-mgmt.conf

# ── dnsmasq proxyPXE ─────────────────────────────────────────────────────────
print_action "Installing dnsmasq proxyPXE config"
ic "$BUILD_DIR/dnsmasq-pxe.conf" /etc/dnsmasq.d/pxe-mgmt.conf 644 /etc/dnsmasq.d/pxe-mgmt.conf

# Disable dnsmasq default config if present (we control via drop-in)
if [[ -f /etc/dnsmasq.conf ]] && ! grep -q "^#.*managed by homelab" /etc/dnsmasq.conf 2>/dev/null; then
    print_sub "Disabling dnsmasq default config"
    mv /etc/dnsmasq.conf /etc/dnsmasq.conf.bak
    echo "# managed by homelab pve-pxe module" > /etc/dnsmasq.conf
fi

# ── nginx vhost ───────────────────────────────────────────────────────────────
print_action "Installing nginx vhost"
ic "$BUILD_DIR/nginx-pxe.conf" /etc/nginx/sites-available/pxe 644 /etc/nginx/sites-available/pxe

if [[ ! -L /etc/nginx/sites-enabled/pxe ]]; then
    ln -sf /etc/nginx/sites-available/pxe /etc/nginx/sites-enabled/pxe
    print_sub "Enabled nginx pxe site"
fi
# Disable default nginx site if enabled
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
    print_sub "Disabled nginx default site"
fi

# ── iPXE menu files ───────────────────────────────────────────────────────────
print_action "Installing iPXE menu files"
ipxe_files=(
    boot.ipxe
    pdm-auto-warning.ipxe
    pdm-auto.ipxe
    pve-load.ipxe
    pve-tui.ipxe
    pve-gui.ipxe
    pve-debug.ipxe
    pve-serial.ipxe
)
for f in "${ipxe_files[@]}"; do
    ic "$BUILD_DIR/$f" "/srv/pxe/$f" 644 "/srv/pxe/$f"
done

# ── TFTP autoexec (dnsmasq:nogroup for tftp-secure) ──────────────────────────
print_action "Installing TFTP autoexec"
ic "$BUILD_DIR/autoexec.ipxe" /srv/tftp/autoexec.ipxe 644 /srv/tftp/autoexec.ipxe
chown dnsmasq:nogroup /srv/tftp/autoexec.ipxe
print_sub "autoexec.ipxe ownership: dnsmasq:nogroup"

# ── PDM answer-auth token ─────────────────────────────────────────────────────
# Staged by the Python orchestrator from 1Password; mode 600; never logged.
TOKEN_SRC="$BUILD_DIR/homelab-pve-auto-install.token"
TOKEN_DST="/root/homelab-pve-auto-install.token"
if [[ -f "$TOKEN_SRC" ]]; then
    print_action "Installing PDM answer-auth token"
    install -m 0600 "$TOKEN_SRC" "$TOKEN_DST"
    print_ok "Token installed at $TOKEN_DST"
else
    if [[ -f "$TOKEN_DST" ]]; then
        print_sub "Token already present on host; skipping (not staged)"
    else
        print_warn "Token not staged and not present on host — run deploy online to install from 1Password"
    fi
fi

# ── Operational scripts ───────────────────────────────────────────────────────
print_action "Installing operational scripts"
ic "$BUILD_DIR/pxe-enable"     /usr/local/sbin/pxe-enable     755 pxe-enable
ic "$BUILD_DIR/pxe-disable"    /usr/local/sbin/pxe-disable    755 pxe-disable
ic "$BUILD_DIR/pxe-autoupdate" /usr/local/sbin/pxe-autoupdate 755 pxe-autoupdate

# ── pxe-autoupdate systemd service + timer ────────────────────────────────────
print_action "Installing pxe-autoupdate systemd units"
ic "$BUILD_DIR/pxe-autoupdate.service" \
    /etc/systemd/system/pxe-autoupdate.service 644 pxe-autoupdate.service
ic "$BUILD_DIR/pxe-autoupdate.timer" \
    /etc/systemd/system/pxe-autoupdate.timer 644 pxe-autoupdate.timer

systemctl daemon-reload || true
systemctl enable pxe-autoupdate.timer
systemctl is-active --quiet pxe-autoupdate.timer || systemctl start pxe-autoupdate.timer
print_ok "pxe-autoupdate.timer enabled"

# ── nginx config test ─────────────────────────────────────────────────────────
nginx -t 2>/dev/null && print_ok "nginx config valid" || {
    print_warn "nginx config test failed — PXE services not started"
    exit 1
}

print_ok "pve-pxe deploy complete"
print_sub "Run: pxe-enable   (to start serving before a rebuild window)"
print_sub "Run: pxe-disable  (to stop serving after a window)"
print_sub "Run: pxe-autoupdate  (to detect and promote a new PVE ISO)"
