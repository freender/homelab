#!/bin/bash
# install.sh - Install PVE/PBS post-install configs
# Usage: ./scripts/install.sh [hostname] [pve|pbs] [timezone]

set -e

HOST=${1:-$(hostname)}
HOST_TYPE=${2:-}
TIMEZONE=${3:-UTC}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
BACKUP_DIR="/var/backups/homelab/pve-postinstall"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    }
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
fi

if [[ -z "$HOST_TYPE" ]]; then
    if command -v pveversion >/dev/null 2>&1; then
        HOST_TYPE="pve"
    elif command -v proxmox-backup-manager >/dev/null 2>&1; then
        HOST_TYPE="pbs"
    fi
fi

required_files_for_type() {
    local host_type="$1"
    case "$host_type" in
        pve)
            printf '%s\n' proxmox.sources pve-enterprise.sources ceph.sources pve-test.sources no-nag-script pve-remove-nag.sh pve-ceph-reconcile.sh
            ;;
        pbs)
            printf '%s\n' proxmox.sources pbs-enterprise.sources no-nag-script pbs-remove-nag.sh
            ;;
        *)
            return 1
            ;;
    esac
}

install_file() {
    local file="$1"
    case "$file" in
        proxmox.sources|pve-enterprise.sources|ceph.sources|pve-test.sources|pbs-enterprise.sources)
            cp "$BUILD_DIR/$file" "/etc/apt/sources.list.d/$file"
            ;;
        no-nag-script)
            cp "$BUILD_DIR/$file" "/etc/apt/apt.conf.d/no-nag-script"
            chmod 644 /etc/apt/apt.conf.d/no-nag-script
            ;;
        pve-remove-nag.sh|pbs-remove-nag.sh)
            mkdir -p /usr/local/bin
            cp "$BUILD_DIR/$file" "/usr/local/bin/$file"
            chmod 755 "/usr/local/bin/$file"
            ;;
        pve-ceph-reconcile.sh)
            mkdir -p /usr/local/sbin
            cp "$BUILD_DIR/$file" "/usr/local/sbin/$file"
            chmod 755 "/usr/local/sbin/$file"
            ;;
        *)
            print_warn "Unsupported file mapping: $file"
            return 1
            ;;
    esac
}

backup_no_nag_script() {
    local src="/etc/apt/apt.conf.d/no-nag-script"
    local ts
    ts="$(date +%Y%m%d%H%M%S)"

    [[ -f "$src" ]] || return 0

    mkdir -p "$BACKUP_DIR"
    cp "$src" "$BACKUP_DIR/no-nag-script.$ts"
}

backup_sources_list_dir() {
    local src="/etc/apt/sources.list.d"
    local ts
    ts="$(date +%Y%m%d%H%M%S)"

    [[ -d "$src" ]] || return 0

    mkdir -p "$BACKUP_DIR"
    cp -r "$src" "$BACKUP_DIR/sources.list.d.$ts"
}

if [[ -z "$HOST_TYPE" ]]; then
    echo "Error: host type not provided and could not be detected"
    exit 1
fi

if [[ ! -d "$BUILD_DIR" ]]; then
    echo "Error: Missing build directory $BUILD_DIR"
    exit 1
fi

print_sub "Backing up repo configs..."
backup_sources_list_dir
backup_no_nag_script

print_sub "Setting timezone to $TIMEZONE..."
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "$TIMEZONE" || print_warn "failed to set timezone to $TIMEZONE"
else
    print_warn "timedatectl not found; timezone not changed"
fi

case "$HOST_TYPE" in
    pve)
        while IFS= read -r file; do
            if [[ ! -f "$BUILD_DIR/$file" ]]; then
                echo "Error: Missing $file in $BUILD_DIR"
                exit 1
            fi
        done < <(required_files_for_type "$HOST_TYPE")

        print_sub "Deploying PVE repo sources..."
        for file in proxmox.sources pve-enterprise.sources ceph.sources pve-test.sources; do
            install_file "$file" || exit 1
        done

        print_sub "Deploying nag removal..."
        install_file pve-remove-nag.sh || exit 1
        install_file no-nag-script || exit 1

        print_sub "Deploying Ceph daemon reconciliation automation..."
        install_file pve-ceph-reconcile.sh || exit 1

        print_sub "Ceph reconciliation available at /usr/local/sbin/pve-ceph-reconcile.sh"

        ;;
    pbs)
        while IFS= read -r file; do
            if [[ ! -f "$BUILD_DIR/$file" ]]; then
                echo "Error: Missing $file in $BUILD_DIR"
                exit 1
            fi
        done < <(required_files_for_type "$HOST_TYPE")

        print_sub "Deploying PBS repo sources..."
        for file in proxmox.sources pbs-enterprise.sources; do
            install_file "$file" || exit 1
        done

        print_sub "Deploying nag removal..."
        install_file pbs-remove-nag.sh || exit 1
        install_file no-nag-script || exit 1
        ;;
    *)
        print_warn "Unsupported host type: $HOST_TYPE"
        exit 1
        ;;
esac

print_sub "Disabling postfix service..."
if systemctl list-unit-files postfix.service >/dev/null 2>&1; then
    systemctl disable --now postfix || print_warn "failed to disable postfix"
else
    print_sub "postfix service not present; skipping"
fi

print_sub "Refreshing proxmox widget toolkit..."
apt --reinstall install proxmox-widget-toolkit &>/dev/null || print_warn "Widget toolkit reinstall failed"

print_sub "Updating system packages..."
apt update &>/dev/null || print_warn "apt update failed"
apt -y dist-upgrade &>/dev/null || print_warn "apt dist-upgrade failed"
