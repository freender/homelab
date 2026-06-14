#!/bin/bash
# install.sh - Deploy HTTP Boot service configs.
# Usage: ./scripts/install.sh [hostname]
# Installs: nginx vhost, HTTP Boot iPXE loader, iPXE menu files, operational
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
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1
load_file_map "$BUILD_DIR/file-map.conf"

print_header "PVE HTTP Boot"

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
command -v rsync    >/dev/null 2>&1 || missing_pkgs+=(rsync)
command -v flock    >/dev/null 2>&1 || missing_pkgs+=(util-linux)
command -v xorriso  >/dev/null 2>&1 || missing_pkgs+=(xorriso)
[[ -f /usr/lib/ipxe/ipxe.efi ]] || missing_pkgs+=(ipxe)
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
mkdir -p /srv/pxe /srv/pxe/httpboot /etc/homelab-pxe /etc/homelab-pxe/iso-answers /srv/pxe/iso \
         /var/lib/node_exporter/textfile \
         /etc/nginx/sites-available /etc/nginx/sites-enabled \
         /root/iso
chmod 700 /etc/homelab-pxe/iso-answers
chmod 755 /srv/pxe/iso

print_action "Cleaning legacy duplicate artifacts"
(
    flock -n 9 || {
        print_warn "pxe-autoupdate is running; skipping artifact cleanup"
        exit 0
    }
    rm -rf /srv/pxe.stage /root/iso/pxe-build /srv/pxe.prev/iso
) 9>/run/pxe-autoupdate.lock

print_action "Installing managed HTTP Boot files"
rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

# ── Management config (PDM URL + fingerprint) ────────────────────────────────
print_action "Installing management config"

# ── Retire legacy proxyPXE/TFTP ──────────────────────────────────────────────
print_action "Disabling legacy proxyPXE/TFTP"
systemctl disable --now dnsmasq 2>/dev/null || true
rm -f /etc/dnsmasq.d/pxe-mgmt.conf
rm -f /srv/tftp/autoexec.ipxe /srv/tftp/ipxe.efi /srv/tftp/snponly.efi /srv/tftp/undionly.kpxe
rmdir /srv/tftp 2>/dev/null || true

# ── nginx vhost ───────────────────────────────────────────────────────────────
print_action "Installing nginx vhost"

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
    require_file "/srv/pxe/$f" "/srv/pxe/$f" || exit 1
done

if [[ -r /etc/homelab-pxe/pxe-mgmt.conf ]]; then
    # shellcheck source=/dev/null
    source /etc/homelab-pxe/pxe-mgmt.conf
fi
current_iso="$(find /srv/pxe -maxdepth 1 -name 'proxmox-ve_*auto*.iso' -printf '%f\n' 2>/dev/null | sort -V | tail -1)"
if [[ -z "$current_iso" ]]; then
    current_iso="$(find /srv/pxe -maxdepth 1 -name 'proxmox-ve_*.iso' -printf '%f\n' 2>/dev/null | sort -V | tail -1)"
fi
if [[ -n "$current_iso" ]]; then
    sed -i -E \
        -e "s#http://[^/]+/proxmox-ve_[^[:space:]]+\.iso#http://${PXE_MGMT_IP:-10.0.0.50}/${current_iso}#g" \
        -e "s#proxmox-ve_PLACEHOLDER\.iso#${current_iso}#g" \
        /srv/pxe/pve-load.ipxe
    print_sub "pve-load.ipxe points at $current_iso"
else
    print_warn "no proxmox-ve_*.iso found under /srv/pxe; pve-load.ipxe left unchanged"
fi

print_action "Installing HTTP Boot loader"
if [[ -f /usr/lib/ipxe/ipxe.efi ]]; then
    install -m 0644 /usr/lib/ipxe/ipxe.efi /srv/pxe/httpboot/ipxe.efi
    print_sub "installed /srv/pxe/httpboot/ipxe.efi"
else
    print_error "missing packaged iPXE binary: /usr/lib/ipxe/ipxe.efi"
    exit 1
fi

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

# ── Baked ISO answer TOML files (0600; staged by Python orchestrator) ─────────
ANSWERS_SRC="$BUILD_DIR/iso-answers"
if [[ -d "$ANSWERS_SRC" ]] && [[ -n "$(ls -A "$ANSWERS_SRC" 2>/dev/null)" ]]; then
    print_action "Installing baked ISO answer TOML files"
    for f in "$ANSWERS_SRC"/*.toml; do
        name="$(basename "$f")"
        install -m 0600 "$f" "/etc/homelab-pxe/iso-answers/$name"
        print_sub "  installed $name"
    done
else
    print_sub "No new baked ISO answer files staged"
fi

# ── pxe-autoupdate systemd service + timer ────────────────────────────────────
print_action "Installing pxe-autoupdate systemd units"

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
    print_warn "nginx config test failed — HTTP Boot service not started"
    exit 1
fi

print_ok "pve-pxe deploy complete"
print_sub "UniFi Network Boot filename: http://10.0.0.50/httpboot/ipxe.efi"
print_sub "Run: pxe-enable   (to ensure nginx is serving HTTP Boot)"
print_sub "Run: pxe-disable  (note: disable UniFi Network Boot to stop clients)"
print_sub "Run: pxe-autoupdate  (to detect and promote a new PVE ISO)"
print_sub "Run: iso-autobuild   (to manually rebuild baked offsite ISOs)"
