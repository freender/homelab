#!/bin/bash
# install.sh - Install PVE post-install configs
# Usage: ./scripts/install.sh [hostname] [pve] [timezone] [ceph_enabled]

set -e

HOST=${1:-$(hostname)}
HOST_TYPE=${2:-}
TIMEZONE=${3:-UTC}
CEPH_ENABLED=${4:-false}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
BACKUP_DIR="/var/backups/homelab/pve-postinstall"
INSTALL_FILE_CHANGED="false"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

if [[ -z "$HOST_TYPE" ]]; then
    if command -v pveversion >/dev/null 2>&1; then
        HOST_TYPE="pve"
    fi
fi

required_files_for_type() {
    local host_type="$1"
    case "$host_type" in
        pve)
            printf '%s\n' proxmox.sources ceph.sources pve-test.sources no-nag-script pve-remove-nag.sh sshd-hardening.conf
            ;;
        *)
            return 1
            ;;
    esac
}

load_file_map() {
    local map_file="$BUILD_DIR/file-map.conf"
    local filename remote_path mode

    require_file "$map_file" "$map_file" || exit 1

    declare -g -A FILE_MAP_DEST=()
    declare -g -A FILE_MAP_MODE=()
    while IFS='|' read -r filename remote_path mode; do
        FILE_MAP_DEST["$filename"]="$remote_path"
        FILE_MAP_MODE["$filename"]="${mode:-644}"
    done < "$map_file"
}

install_file() {
    local file="$1"
    local source_file="$BUILD_DIR/$file"
    local destination_file
    local mode
    local rc

    INSTALL_FILE_CHANGED="false"

    if [[ -z "${FILE_MAP_DEST[$file]+x}" ]]; then
        print_warn "no mapping for file: $file"
        return 1
    fi

    destination_file="${FILE_MAP_DEST[$file]}"
    mode="${FILE_MAP_MODE[$file]:-644}"

    mkdir -p "$(dirname "$destination_file")"

    file_needs_update "$source_file" "$destination_file"
    rc=$?
    if [[ $rc -ne 0 ]]; then
        if [[ $rc -eq 1 ]]; then
            print_sub "$destination_file unchanged; skipping update"
            chmod "$mode" "$destination_file"
            return 0
        fi
        return "$rc"
    fi

    cp "$source_file" "$destination_file"
    chmod "$mode" "$destination_file"
    INSTALL_FILE_CHANGED="true"
    print_sub "Updated $destination_file"
}

repo_files_need_backup() {
    local file
    for file in proxmox.sources ceph.sources pve-test.sources; do
        if [[ ! -e "/etc/apt/sources.list.d/$file" ]] || ! cmp -s "$BUILD_DIR/$file" "/etc/apt/sources.list.d/$file"; then
            return 0
        fi
    done
    return 1
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

ensure_local_zfs_storage() {
    if ! command -v pvesm >/dev/null 2>&1; then
        print_warn "pvesm not found; skipping zfs storage reconciliation"
        return 0
    fi

    if ! command -v zpool >/dev/null 2>&1; then
        print_warn "zpool not found; skipping zfs storage reconciliation"
        return 0
    fi

    if ! zpool list rpool >/dev/null 2>&1; then
        print_warn "rpool not found; skipping local-zfs reconciliation"
        return 0
    fi

    if pvesm status --storage local-zfs >/dev/null 2>&1; then
        print_sub "local-zfs storage already configured"
        return 0
    fi

    print_sub "Creating local-zfs storage on rpool..."
    pvesm add zfspool local-zfs --pool rpool --content images,rootdir --sparse 0 || print_warn "failed to create local-zfs storage"
}

install_other_subfeatures() {
    if [[ -f "$BUILD_DIR/interfaces" ]]; then
        print_sub "Configuring network interfaces..."
        bash "$SCRIPT_DIR/scripts/install-interfaces.sh" "$HOST" || return 1
    else
        print_sub "Network interfaces not configured; skipping"
    fi
}

if [[ -z "$HOST_TYPE" ]]; then
    print_error "host type not provided and could not be detected"
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

load_file_map

print_sub "Checking if repo configs need backup..."
if repo_files_need_backup; then
    print_sub "Backing up /etc/apt/sources.list.d..."
    backup_sources_list_dir
else
    print_sub "/etc/apt/sources.list.d unchanged; skipping backup"
fi

if [[ ! -e "/etc/apt/apt.conf.d/no-nag-script" ]] || ! cmp -s "$BUILD_DIR/no-nag-script" "/etc/apt/apt.conf.d/no-nag-script"; then
    print_sub "Backing up no-nag-script..."
    backup_no_nag_script
else
    print_sub "no-nag-script unchanged; skipping backup"
fi

print_sub "Removing enterprise repository definitions..."
rm -f /etc/apt/sources.list.d/pve-enterprise.sources
rm -f /etc/apt/sources.list.d/ceph.list
rm -f /etc/apt/sources.list.d/ceph-enterprise.list

print_sub "Setting timezone to $TIMEZONE..."
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "$TIMEZONE" || print_warn "failed to set timezone to $TIMEZONE"
else
    print_warn "timedatectl not found; timezone not changed"
fi

if [[ -e "/usr/share/zoneinfo/$TIMEZONE" ]]; then
    ln -snf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime || print_warn "failed to update /etc/localtime"
    printf '%s\n' "$TIMEZONE" > /etc/timezone || print_warn "failed to write /etc/timezone"
else
    print_warn "timezone data not found for $TIMEZONE"
fi

case "$HOST_TYPE" in
    pve)
        while IFS= read -r file; do
            if [[ ! -f "$BUILD_DIR/$file" ]]; then
                print_error "Missing $file in $BUILD_DIR"
                exit 1
            fi
        done < <(required_files_for_type "$HOST_TYPE")

        print_sub "Deploying PVE repo sources..."
        for file in proxmox.sources ceph.sources pve-test.sources; do
            install_file "$file" || exit 1
        done

        print_sub "Deploying nag removal..."
        install_file pve-remove-nag.sh || exit 1
        if [[ "$INSTALL_FILE_CHANGED" == "true" ]]; then
            nag_changed=true
        fi
        install_file no-nag-script || exit 1
        if [[ "$INSTALL_FILE_CHANGED" == "true" ]]; then
            nag_changed=true
        fi

        print_sub "Deploying sshd hardening config..."
        if install_file sshd-hardening.conf; then
            if sshd -t 2>/dev/null; then
                systemctl reload sshd && print_sub "sshd reloaded with hardened config"
            else
                print_warn "sshd -t failed; sshd not reloaded"
            fi
        else
            exit 1
        fi

        if [[ "$CEPH_ENABLED" == "true" ]]; then
            print_sub "Running Ceph daemon reconciliation..."
            bash "$SCRIPT_DIR/scripts/pve-ceph-reconcile.sh" || print_warn "ceph daemon reconciliation skipped"
        else
            print_sub "Ceph feature disabled for host; skipping Ceph reconciliation"
        fi

        print_sub "Reconciling local ZFS storage..."
        ensure_local_zfs_storage

        print_sub "Applying additional subfeatures..."
        install_other_subfeatures || exit 1
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

if [[ "${nag_changed:-false}" == "true" ]]; then
    print_sub "Refreshing proxmox widget toolkit..."
    if ! apt --reinstall install proxmox-widget-toolkit >/dev/null 2>&1; then
        print_warn "Widget toolkit reinstall failed; run manually: apt --reinstall install proxmox-widget-toolkit"
    fi
else
    print_sub "Nag files unchanged; skipping widget toolkit reinstall"
fi
