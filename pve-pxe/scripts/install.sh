#!/bin/bash
# install.sh - Deploy PXE service configs to saint (CT 112 on osiris).
# Usage: ./scripts/install.sh [hostname]
# Installs: dnsmasq proxyPXE config, nginx vhost, iPXE menu files, operational
#           scripts (pxe-enable, pxe-disable, pxe-autoupdate, iso-autobuild),
#           pxe-autoupdate systemd service/timer, and baked ISO answer files.
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

bootstrap_pkgs=()
command -v curl >/dev/null 2>&1 || bootstrap_pkgs+=(curl)
[[ -f /etc/ssl/certs/ca-certificates.crt ]] || bootstrap_pkgs+=(ca-certificates)
if [[ ${#bootstrap_pkgs[@]} -gt 0 ]]; then
    print_sub "Installing bootstrap packages: ${bootstrap_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -qq "${bootstrap_pkgs[@]}"
fi

# ── Proxmox no-subscription repo (needed for proxmox-auto-install-assistant) ──
PROXMOX_REPO="/etc/apt/sources.list.d/proxmox-pve.list"
PROXMOX_KEY="/etc/apt/trusted.gpg.d/proxmox-release-trixie.gpg"
if [[ ! -f "$PROXMOX_REPO" ]]; then
    print_action "Adding Proxmox no-subscription apt repo"
    curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg \
        -o "$PROXMOX_KEY" \
        || { echo "WARN: could not fetch Proxmox GPG key; repo not added" >&2; }
    if [[ -f "$PROXMOX_KEY" ]]; then
        echo "deb http://download.proxmox.com/debian/pve trixie pve-no-subscription" \
            > "$PROXMOX_REPO"
        apt-get update -qq
        print_ok "Proxmox repo added"
    fi
else
    print_sub "Proxmox repo already configured"
fi

# ── Packages ────────────────────────────────────────────────────────────────
missing_pkgs=()
command -v nginx    >/dev/null 2>&1 || missing_pkgs+=(nginx)
command -v dnsmasq  >/dev/null 2>&1 || missing_pkgs+=(dnsmasq)
command -v rsync    >/dev/null 2>&1 || missing_pkgs+=(rsync)
command -v flock    >/dev/null 2>&1 || missing_pkgs+=(util-linux)
command -v xorriso  >/dev/null 2>&1 || missing_pkgs+=(xorriso)
command -v proxmox-auto-install-assistant >/dev/null 2>&1 \
    || missing_pkgs+=(proxmox-auto-install-assistant)
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    print_sub "Installing packages: ${missing_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing_pkgs[@]}"
else
    print_sub "All required packages already installed"
fi

# ── Directory layout ─────────────────────────────────────────────────────────
mkdir -p /srv/pxe /srv/tftp /etc/saint /etc/saint/iso-answers /srv/pxe/iso \
         /var/lib/node_exporter/textfile \
         /etc/nginx/sites-available /etc/nginx/sites-enabled \
         /etc/dnsmasq.d /root/iso
chmod 700 /etc/saint/iso-answers
chmod 755 /srv/pxe/iso

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
ic "$BUILD_DIR/iso-autobuild"  /usr/local/sbin/iso-autobuild  755 iso-autobuild

# ── Baked ISO answer TOML files (0600; staged by Python orchestrator) ─────────
ANSWERS_SRC="$BUILD_DIR/iso-answers"
if [[ -d "$ANSWERS_SRC" ]] && [[ -n "$(ls -A "$ANSWERS_SRC" 2>/dev/null)" ]]; then
    print_action "Installing baked ISO answer TOML files"
    for f in "$ANSWERS_SRC"/*.toml; do
        name="$(basename "$f")"
        install -m 0600 "$f" "/etc/saint/iso-answers/$name"
        print_sub "  installed $name"
    done
else
    print_sub "No new baked ISO answer files staged"
fi

# ── pxe-autoupdate systemd service + timer ────────────────────────────────────
print_action "Installing pxe-autoupdate systemd units"
ic "$BUILD_DIR/pxe-autoupdate.service" \
    /etc/systemd/system/pxe-autoupdate.service 644 pxe-autoupdate.service
ic "$BUILD_DIR/pxe-autoupdate.timer" \
    /etc/systemd/system/pxe-autoupdate.timer 644 pxe-autoupdate.timer
ic "$BUILD_DIR/iso-autobuild.service" \
    /etc/systemd/system/iso-autobuild.service 644 iso-autobuild.service

if systemctl list-unit-files --no-legend iso-autobuild.timer 2>/dev/null \
    | grep -q '^iso-autobuild\.timer'; then
    systemctl disable --now iso-autobuild.timer || true
    rm -f /etc/systemd/system/iso-autobuild.timer
    print_sub "Removed legacy iso-autobuild.timer"
fi

systemctl daemon-reload || true
systemctl enable pxe-autoupdate.timer
systemctl is-active --quiet pxe-autoupdate.timer || systemctl start pxe-autoupdate.timer
print_ok "pxe-autoupdate.timer enabled"

# ── nginx config test + reload ────────────────────────────────────────────────
if nginx -t 2>/dev/null; then
    print_ok "nginx config valid"
    # Use restart (not reload) so new listen directives take effect.
    systemctl is-active --quiet nginx && systemctl restart nginx || true
else
    print_warn "nginx config test failed — PXE services not started"
    exit 1
fi

print_ok "pve-pxe deploy complete"
print_sub "Run: pxe-enable   (to start serving before a rebuild window)"
print_sub "Run: pxe-disable  (to stop serving after a window)"
print_sub "Run: pxe-autoupdate  (to detect and promote a new PVE ISO)"
print_sub "Run: iso-autobuild   (to manually rebuild baked offsite ISOs)"
