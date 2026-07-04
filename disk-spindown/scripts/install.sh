#!/bin/bash

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo -n env FORCE_UPDATE="${FORCE_UPDATE:-false}" bash "$0" "$@"
fi

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

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

print_header "Disk Spindown"

print_action "Package"
if ! command -v hd-idle >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q hd-idle
    print_ok "hd-idle installed"
else
    print_sub "hd-idle already installed"
fi

config_changed=false
units_changed=false

discover_hdd_devices() {
    lsblk -dn -o NAME,TYPE,ROTA | while read -r name type rota; do
        [[ "$type" == "disk" ]] || continue
        [[ "$rota" == "1" ]] || continue
        [[ -b "/dev/$name" ]] || continue
        printf '/dev/%s\n' "$name"
    done
}

write_discovered_defaults() {
    local defaults_path="/etc/default/homelab-disk-spindown"
    local tmp_path
    local devices=()
    local opts
    local device

    # shellcheck source=/dev/null
    source "$defaults_path"

    mapfile -t devices < <(discover_hdd_devices)
    if [[ ${#devices[@]} -eq 0 ]]; then
        print_error "No rotational disks discovered for disk spindown"
        return 1
    fi

    opts="-i 0 -c ${HD_IDLE_COMMAND_TYPE:-ata} -s ${HD_IDLE_SYMLINK_POLICY:-1}"
    for device in "${devices[@]}"; do
        opts+=" -a $device -i ${HD_IDLE_IDLE_SECONDS:-1800}"
    done

    tmp_path="$(mktemp)"
    {
        printf 'HD_IDLE_ENABLED="%s"\n' "${HD_IDLE_ENABLED:-true}"
        printf 'HD_IDLE_IDLE_SECONDS="%s"\n' "${HD_IDLE_IDLE_SECONDS:-1800}"
        printf 'HD_IDLE_COMMAND_TYPE="%s"\n' "${HD_IDLE_COMMAND_TYPE:-ata}"
        printf 'HD_IDLE_SYMLINK_POLICY="%s"\n' "${HD_IDLE_SYMLINK_POLICY:-1}"
        printf 'HD_IDLE_OPTS="%s"\n' "$opts"
    } > "$tmp_path"

    if ! cmp -s "$tmp_path" "$defaults_path"; then
        install -m 0644 "$tmp_path" "$defaults_path"
        config_changed=true
        print_ok "Discovered HDDs: ${devices[*]}"
    else
        print_sub "Discovered HDDs unchanged: ${devices[*]}"
    fi
    rm -f "$tmp_path"
}

for file_name in \
    homelab-disk-spindown.defaults \
    homelab-disk-spindown.service \
    homelab-disk-wakeup \
    homelab-disk-wakeup.service \
    homelab-disk-wakeup.timer; do
    rc=0
    install_build_file "$file_name" || rc=$?
    [[ $rc -eq 0 ]] && config_changed=true
    if [[ $rc -eq 0 ]]; then
        case "$file_name" in
            *.service | *.timer) units_changed=true ;;
        esac
    fi
done

# Check the enabled/paused flag from the just-installed defaults file before
# running hardware discovery. Discovery (write_discovered_defaults) can fail
# hard if no rotational disks are currently visible (e.g. disks temporarily
# disconnected/being serviced), which is exactly a scenario where pausing
# matters. Skip discovery entirely when paused so disabling never depends on
# disk enumeration succeeding.
# shellcheck disable=SC1091
source /etc/default/homelab-disk-spindown

if [[ "${HD_IDLE_ENABLED:-true}" == "false" ]]; then
    if [[ "$units_changed" == "true" ]]; then
        systemctl daemon-reload
    fi

    systemctl disable --now hd-idle.service 2>/dev/null || true

    homelab_apply_pause "true" \
        homelab-disk-wakeup.timer \
        homelab-disk-spindown.service

    print_header "Disk Spindown Complete (paused)"
    exit 0
fi

write_discovered_defaults

if [[ "$units_changed" == "true" ]]; then
    systemctl daemon-reload
fi

systemctl disable --now hd-idle.service 2>/dev/null || true

if ! systemctl is-enabled --quiet homelab-disk-wakeup.timer 2>/dev/null; then
    systemctl enable --now homelab-disk-wakeup.timer
    print_ok "homelab-disk-wakeup.timer enabled"
elif [[ "$config_changed" == "true" ]]; then
    systemctl restart homelab-disk-wakeup.timer
    print_ok "homelab-disk-wakeup.timer restarted"
elif ! systemctl is-active --quiet homelab-disk-wakeup.timer 2>/dev/null; then
    systemctl start homelab-disk-wakeup.timer
    print_ok "homelab-disk-wakeup.timer started"
else
    print_sub "homelab-disk-wakeup.timer already enabled"
fi

if ! systemctl is-enabled --quiet homelab-disk-spindown.service 2>/dev/null; then
    systemctl enable --now homelab-disk-spindown.service
    print_ok "homelab-disk-spindown.service enabled"
elif [[ "$config_changed" == "true" ]]; then
    systemctl restart homelab-disk-spindown.service
    print_ok "homelab-disk-spindown.service restarted"
elif ! systemctl is-active --quiet homelab-disk-spindown.service 2>/dev/null; then
    systemctl start homelab-disk-spindown.service
    print_ok "homelab-disk-spindown.service started"
else
    print_sub "homelab-disk-spindown.service already enabled"
fi

print_header "Disk Spindown Complete"
