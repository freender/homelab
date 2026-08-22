#!/bin/bash
# install.sh - Deploy HTTP Boot service configs.
# Usage: ./scripts/install.sh [hostname]
# Installs: nginx vhost, HTTP Boot iPXE loader, iPXE menu files, operational
#           scripts, and the pve-http-boot-autoupdate systemd service/timer.
# Does NOT stage the Proxmox ISO or initrd — those are managed at runtime by pve-http-boot-autoupdate.

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
[[ -f /usr/lib/ipxe/snponly.efi ]] || missing_pkgs+=(ipxe)
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
mkdir -p /srv/httpboot /srv/httpboot/httpboot /etc/homelab-http-boot \
         /etc/nginx/sites-available /etc/nginx/sites-enabled \
         /root/iso

# Retired: pve-http-boot-autoupdate used to write node_exporter
# textfile metrics here, but this host has no node_exporter to ever read them
# (it's outside the metrics-exporters host list). Remove any stale metric files
# from earlier deploys.
rm -rf /var/lib/node_exporter/textfile

print_action "Cleaning duplicate artifacts"
(
    flock -n 9 || {
        print_warn "pve-http-boot-autoupdate is running; skipping artifact cleanup"
        exit 0
    }
    rm -rf /srv/httpboot.stage /root/iso/http-boot-build /srv/httpboot.prev/iso
) 9>/run/pve-http-boot-autoupdate.lock

print_action "Installing managed HTTP Boot files"
rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

# ── Retire legacy proxyPXE/TFTP ──────────────────────────────────────────────
print_action "Disabling legacy proxyPXE/TFTP"
systemctl disable --now dnsmasq 2>/dev/null || true
rm -f /srv/tftp/autoexec.ipxe /srv/tftp/ipxe.efi /srv/tftp/snponly.efi /srv/tftp/undionly.kpxe
rmdir /srv/tftp 2>/dev/null || true

# ── Retire baked offsite install ISOs ────────────────────────────────────────
# The baked-ISO builder existed only for the offsite hosts (cinci, cottonwood)
# while they ran Proxmox VE. They are Ubuntu now, so the builder, its answer
# files, and the served /iso/ tree are retired. Answer TOMLs carry a hashed root
# password, so shred them rather than plain rm.
if [[ -e /srv/httpboot/iso || -e /etc/homelab-http-boot/iso-answers \
      || -e /usr/local/sbin/iso-autobuild || -e /etc/systemd/system/iso-autobuild.service ]]; then
    print_action "Retiring baked offsite ISO build"
    systemctl disable --now iso-autobuild.service 2>/dev/null || true
    rm -f /etc/systemd/system/iso-autobuild.service /etc/systemd/system/iso-autobuild.timer
    rm -f /usr/local/sbin/iso-autobuild
    if [[ -d /etc/homelab-http-boot/iso-answers ]]; then
        find /etc/homelab-http-boot/iso-answers -type f -exec shred -u {} + 2>/dev/null || true
        rm -rf /etc/homelab-http-boot/iso-answers
    fi
    rm -rf /srv/httpboot/iso /srv/httpboot.prev/iso /srv/httpboot.stage/iso
    systemctl daemon-reload || true
    print_sub "Removed baked ISO builder, answers, and /srv/httpboot/iso"
fi

# ── nginx vhost ───────────────────────────────────────────────────────────────
print_action "Installing nginx vhost"

rm -f /etc/nginx/sites-enabled/pxe
if [[ ! -L /etc/nginx/sites-enabled/http-boot ]]; then
    ln -sf /etc/nginx/sites-available/http-boot /etc/nginx/sites-enabled/http-boot
    print_sub "Enabled nginx http-boot site"
fi
# Disable default nginx site if enabled
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
    print_sub "Disabled nginx default site"
fi

# ── Retire the hand-rolled iPXE menus ────────────────────────────────────────
# The boot menu is now the stock one emitted by `proxmox-auto-install-assistant
# prepare-iso --pxe-loader ipxe` and installed at runtime by
# pve-http-boot-autoupdate, so the deploy no longer ships menus or rewrites a
# PLACEHOLDER ISO name into them. Remove the superseded files; leaving them
# served would offer boot entries that still carry pre-8.2 kernel args.
print_action "Removing superseded hand-rolled iPXE menus"
rm -f /srv/httpboot/pve-load.ipxe /srv/httpboot/pdm-auto.ipxe \
      /srv/httpboot/pdm-auto-warning.ipxe /srv/httpboot/pve-tui.ipxe \
      /srv/httpboot/pve-gui.ipxe /srv/httpboot/pve-debug.ipxe \
      /srv/httpboot/pve-serial.ipxe

# The old homelab boot.ipxe chained to the menus just removed, so leaving it in
# place would serve a menu whose every installer entry dead-ends. Match on its
# content rather than removing unconditionally: after migration this same path
# holds the stock Proxmox menu, and deleting that on every re-deploy would break
# netboot until the next autoupdate run.
if [[ -f /srv/httpboot/boot.ipxe ]] \
   && grep -q "Homelab Network Boot" /srv/httpboot/boot.ipxe; then
    rm -f /srv/httpboot/boot.ipxe
    print_sub "removed superseded homelab boot menu"
fi

# Serve snponly.efi, not ipxe.efi. ipxe.efi carries iPXE's own NIC drivers and
# resets the card when it takes over, so the link drops and must renegotiate.
# That is free on a virtio NIC and has never once succeeded on this fleet's
# bare metal: every node here boots from an HP 560SFP+ (Intel 82599), where
# iPXE re-inits the card and then sits on "Waiting for link-up" until it gives
# up. arc's access log records two VM boots completing the full chain and zero
# bare-metal ones. snponly.efi instead binds to the UEFI Simple Network
# Protocol, reusing the option-ROM driver that already brought the link up and
# fetched this very file - no reset, no relink.
#
# The destination filename stays ipxe.efi deliberately: it is the URL baked
# into the UniFi Network Boot DHCP option, and renaming it would strand every
# client until that option is edited by hand.
print_action "Installing HTTP Boot loader"
if [[ -f /usr/lib/ipxe/snponly.efi ]]; then
    install -m 0644 /usr/lib/ipxe/snponly.efi /srv/httpboot/httpboot/ipxe.efi
    print_sub "installed /srv/httpboot/httpboot/ipxe.efi (from snponly.efi, SNP-bound)"
else
    print_error "missing packaged iPXE binary: /usr/lib/ipxe/snponly.efi"
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

# ── pve-http-boot-autoupdate systemd service + timer ──────────────────────────
print_action "Installing pve-http-boot-autoupdate systemd units"

systemctl daemon-reload || true
systemctl enable pve-http-boot-autoupdate.timer
systemctl is-active --quiet pve-http-boot-autoupdate.timer || systemctl start pve-http-boot-autoupdate.timer
print_ok "pve-http-boot-autoupdate.timer enabled"

# ── nginx config test + reload ────────────────────────────────────────────────
if nginx -t 2>/dev/null; then
    print_ok "nginx config valid"
    # Use restart (not reload) so new listen directives take effect.
    systemctl is-active --quiet nginx && systemctl restart nginx || true
else
    print_warn "nginx config test failed — HTTP Boot service not started"
    exit 1
fi

# ── Ensure a payload exists ───────────────────────────────────────────────────
# boot.ipxe, vmlinuz, initrd.img and the prepared ISO are all built at runtime by
# pve-http-boot-autoupdate, so a fresh host (or one migrating off the hand-rolled
# menus) has nothing to serve until it runs. Waiting for the timer would leave
# netboot dropping to an iPXE shell for up to a week. Kick it off detached: the
# build takes minutes and its result belongs in the journal, not in deploy output.
if [[ ! -s /srv/httpboot/boot.ipxe ]]; then
    print_action "No boot payload present; starting pve-http-boot-autoupdate"
    systemctl start --no-block pve-http-boot-autoupdate.service || true
    print_sub "Watch: journalctl -fu pve-http-boot-autoupdate.service"
fi

print_ok "pve-http-boot deploy complete"
if [[ -r /etc/homelab-http-boot/http-boot-mgmt.conf ]]; then
    # shellcheck source=/dev/null
    source /etc/homelab-http-boot/http-boot-mgmt.conf
fi
print_sub "UniFi Network Boot filename: http://${HTTP_BOOT_MGMT_IP:-10.0.0.50}/httpboot/ipxe.efi"
print_sub "Run: pve-http-boot-enable   (to ensure nginx is serving HTTP Boot)"
print_sub "Run: pve-http-boot-disable  (note: disable UniFi Network Boot to stop clients)"
print_sub "Run: pve-http-boot-autoupdate  (to detect and promote a new PVE ISO)"
