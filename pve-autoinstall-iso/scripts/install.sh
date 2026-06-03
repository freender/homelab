#!/bin/bash
# install.sh - Deploy ISO build infrastructure to saint.
# Installs: Proxmox apt repo, proxmox-auto-install-assistant, per-host answer
#           TOML files, iso-autobuild script, and systemd timer.
# nginx /iso/ location is managed by the pve-pxe module's nginx-pxe.conf.

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

print_header "PVE Autoinstall ISO (saint)"

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

# ── packages ──────────────────────────────────────────────────────────────────
missing_pkgs=()
command -v xorriso >/dev/null 2>&1 || missing_pkgs+=(xorriso)
command -v proxmox-auto-install-assistant >/dev/null 2>&1 \
    || missing_pkgs+=(proxmox-auto-install-assistant)
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    print_action "Installing packages: ${missing_pkgs[*]}"
    apt-get install -y -qq "${missing_pkgs[@]}"
    print_ok "Packages installed"
else
    print_sub "Required packages already installed"
fi

# ── directories ───────────────────────────────────────────────────────────────
mkdir -p /etc/saint/iso-answers /srv/pxe/iso
chmod 700 /etc/saint/iso-answers
chmod 755 /srv/pxe/iso

# ── answer TOML files (0600; staged by Python orchestrator from riven) ────────
ANSWERS_SRC="$BUILD_DIR/answers"
if [[ -d "$ANSWERS_SRC" ]] && [[ -n "$(ls -A "$ANSWERS_SRC" 2>/dev/null)" ]]; then
    print_action "Installing answer TOML files"
    for f in "$ANSWERS_SRC"/*.toml; do
        name="$(basename "$f")"
        install -m 0600 "$f" "/etc/saint/iso-answers/$name"
        print_sub "  installed $name"
    done
else
    print_sub "No new answer files staged (already present or --force not used)"
fi

# ── iso-autobuild script ──────────────────────────────────────────────────────
ic() { local rc=0; install_if_changed "$@" || rc=$?; [[ $rc -le 1 ]] || return "$rc"; }

print_action "Installing iso-autobuild script"
ic "$BUILD_DIR/iso-autobuild" /usr/local/sbin/iso-autobuild 755 iso-autobuild

# ── systemd units ─────────────────────────────────────────────────────────────
print_action "Installing iso-autobuild systemd units"
ic "$BUILD_DIR/iso-autobuild.service" \
    /etc/systemd/system/iso-autobuild.service 644 iso-autobuild.service
ic "$BUILD_DIR/iso-autobuild.timer" \
    /etc/systemd/system/iso-autobuild.timer 644 iso-autobuild.timer

systemctl daemon-reload || true
systemctl enable iso-autobuild.timer
systemctl is-active --quiet iso-autobuild.timer \
    || systemctl start iso-autobuild.timer
print_ok "iso-autobuild.timer enabled"

print_ok "pve-autoinstall-iso deploy complete"
print_sub "ISOs will be built weekly by iso-autobuild.timer"
print_sub "Run manually: iso-autobuild"
print_sub "Download: https://iso.freender.net/"
