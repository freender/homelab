#!/bin/bash
# install.sh - Install PBS client backup script and timer

set -e

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
load_file_map

print_header "PBS Client Backup"

for required in \
    homelab-pbs-client-backup \
    homelab-pbs-client-backup.conf \
    homelab-pbs-client-backup.service \
    homelab-pbs-client-backup.timer; do
    require_file "$BUILD_DIR/$required" "$BUILD_DIR/$required" || exit 1
done

# shellcheck source=/dev/null
source "$BUILD_DIR/homelab-pbs-client-backup.conf"

destination_count="${DESTINATION_COUNT:-0}"
if [[ ! "$destination_count" =~ ^[1-9][0-9]*$ ]]; then
    print_error "DESTINATION_COUNT must be a positive integer"
    exit 1
fi
for ((i = 0; i < destination_count; i++)); do
    require_file "$BUILD_DIR/destination-$i.env" "$BUILD_DIR/destination-$i.env" || exit 1
    install -m 0600 "$BUILD_DIR/destination-$i.env" "/etc/homelab/pbs-client-backup-destination-$i.env"
done

# Ensure proxmox-backup-client is present.
#  - PVE hosts: ships via the PVE apt repo (managed by pve-postinstall); presence check only.
#  - Ubuntu hosts: install natively from the public, no-subscription Proxmox
#    pbs-client apt repo, pinning the matching Debian suite (Ubuntu has no suite
#    upstream, but the packages are ABI-compatible with the mapped Debian release).
KEYRING_DIR="/usr/share/keyrings"
PBS_CLIENT_SOURCE="/etc/apt/sources.list.d/pbs-client.sources"

ubuntu_pbs_suite() {
    # Map Ubuntu release -> Debian suite published in download.proxmox.com/debian/pbs-client.
    local version_id="" codename=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        version_id="${VERSION_ID:-}"
        codename="${VERSION_CODENAME:-}"
    fi
    case "$version_id" in
        26.04) echo "trixie"; return 0 ;;
        24.04) echo "bookworm"; return 0 ;;
    esac
    # Fallback by codename for releases not explicitly mapped above.
    case "$codename" in
        resolute) echo "trixie"; return 0 ;;
        noble) echo "bookworm"; return 0 ;;
    esac
    return 1
}

install_pbs_client_ubuntu() {
    local suite keyring src_keyring
    if ! suite="$(ubuntu_pbs_suite)"; then
        print_error "Unable to map this Ubuntu release to a Proxmox pbs-client suite"
        return 1
    fi
    keyring="$KEYRING_DIR/proxmox-release-${suite}.gpg"
    src_keyring="$SCRIPT_DIR/configs/keyrings/proxmox-release-${suite}.gpg"

    if [[ ! -f "$src_keyring" ]]; then
        print_error "Missing vendored keyring: $src_keyring"
        return 1
    fi

    local changed=false
    if [[ ! -f "$keyring" ]] || ! cmp -s "$src_keyring" "$keyring"; then
        install -m 0644 "$src_keyring" "$keyring"
        print_sub "Installed Proxmox $suite release keyring"
        changed=true
    fi

    local desired
    desired=$(cat <<EOF
Types: deb
URIs: http://download.proxmox.com/debian/pbs-client
Suites: $suite
Components: main
Signed-By: $keyring
EOF
)
    if [[ ! -f "$PBS_CLIENT_SOURCE" ]] || [[ "$(cat "$PBS_CLIENT_SOURCE")" != "$desired" ]]; then
        printf '%s\n' "$desired" > "$PBS_CLIENT_SOURCE"
        chmod 0644 "$PBS_CLIENT_SOURCE"
        print_sub "Wrote $PBS_CLIENT_SOURCE (suite: $suite)"
        changed=true
    fi

    if [[ "$changed" == true ]] || ! command -v proxmox-backup-client >/dev/null 2>&1; then
        print_sub "Updating apt (pbs-client) and installing proxmox-backup-client..."
        apt-get update -o Dir::Etc::sourcelist="$PBS_CLIENT_SOURCE" \
            -o Dir::Etc::sourceparts="-" -o APT::Get::List-Cleanup="0" >/dev/null
        DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-backup-client >/dev/null
    fi

    if ! command -v proxmox-backup-client >/dev/null 2>&1; then
        print_error "proxmox-backup-client not found after install"
        return 1
    fi
    print_ok "proxmox-backup-client present ($(proxmox-backup-client version 2>/dev/null | head -1))"
}

case "${HOST_TYPE:-}" in
    ubuntu)
        install_pbs_client_ubuntu || exit 1
        ;;
    pve)
        if ! command -v proxmox-backup-client >/dev/null 2>&1; then
            print_error "proxmox-backup-client not found (expected from PVE repo)"
            exit 1
        fi
        ;;
    *)
        print_error "Unsupported HOST_TYPE: ${HOST_TYPE:-} (expected 'ubuntu' or 'pve')"
        exit 1
        ;;
esac

archive_count="${ARCHIVE_COUNT:-0}"
if [[ "$archive_count" =~ ^[0-9]+$ ]]; then
    for ((i = 0; i < archive_count; i++)); do
        dataset_var="ARCHIVE_${i}_DATASET"
        if [[ -n "${!dataset_var:-}" ]] && ! command -v zfs >/dev/null 2>&1; then
            print_error "zfs command not found"
            exit 1
        fi
    done
fi

# Client-side encryption keyfile: staged from the tmpfs secret cache when
# pbs-client-backup.encrypt is true. Placed at KEYFILE (0600) so backup (and,
# for PVE hosts, the pve-backup config restore) can encrypt/decrypt.
if [[ "${ENCRYPT:-false}" == "true" ]]; then
    staged_keyfile="$BUILD_DIR/pbs-encryption.key"
    if [[ ! -f "$staged_keyfile" ]]; then
        print_error "ENCRYPT=true but staged keyfile missing: $staged_keyfile"
        print_sub "Run ./deploy pbs-client-backup $HOST from riven so the key is staged from 1Password"
        exit 1
    fi
    keyfile_dest="${KEYFILE:-/etc/homelab/pbs-encryption.key}"
    mkdir -p "$(dirname "$keyfile_dest")"
    if [[ ! -f "$keyfile_dest" ]] || ! cmp -s "$staged_keyfile" "$keyfile_dest"; then
        install -m 0600 "$staged_keyfile" "$keyfile_dest"
        print_sub "Installed PBS encryption keyfile at $keyfile_dest"
    fi
fi

changed=false
rc=0
install_file_map || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
[[ $rc -eq 0 ]] && changed=true

homelab_reload_and_clear_failed "$changed" homelab-pbs-client-backup.service

if [[ "${RETIRE_PVE_CONFIG_BACKUP:-false}" == "true" ]]; then
    retired_changed=false
    for timer in pve-config-backup.timer homelab-pve-config-backup.timer; do
        if systemctl is-enabled --quiet "$timer" 2>/dev/null; then
            systemctl disable --now "$timer" >/dev/null
            print_sub "Retired $timer disabled"
            retired_changed=true
        elif systemctl is-active --quiet "$timer" 2>/dev/null; then
            systemctl stop "$timer" >/dev/null
            print_sub "Retired $timer stopped"
            retired_changed=true
        fi
    done
    for retired_file in \
        /root/pve-config-backup.sh \
        /etc/homelab/pve-config-backup.env \
        /etc/systemd/system/pve-config-backup.service \
        /etc/systemd/system/pve-config-backup.timer \
        /etc/systemd/system/homelab-pve-config-backup.service \
        /etc/systemd/system/homelab-pve-config-backup.timer; do
        if [[ -e "$retired_file" ]]; then
            rm -f "$retired_file"
            retired_changed=true
        fi
    done
    if [[ "$retired_changed" == true ]]; then
        systemctl daemon-reload
    fi
fi

# Paused: keep the backup script/units installed and current, but stop and
# disable the timer so no backups run. Flip pbs-client-backup.paused back to
# false to resume.
pause_rc=0
homelab_apply_pause "${PAUSED:-false}" homelab-pbs-client-backup.timer || pause_rc=$?
if [[ $pause_rc -eq 0 ]]; then
    print_header "PBS Client Backup paused"
    exit 0
fi
[[ $pause_rc -eq 1 ]] || exit "$pause_rc"

systemctl enable --now homelab-pbs-client-backup.timer >/dev/null
print_ok "homelab-pbs-client-backup.timer enabled"

systemctl list-timers homelab-pbs-client-backup.timer --no-pager --all || true
print_ok "PBS client backup installed"
