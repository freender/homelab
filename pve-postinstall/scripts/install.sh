#!/bin/bash
# install.sh - Install PVE post-install configs
# Usage: ./scripts/install.sh [hostname] [pve] [timezone] [import_pools] [mounts] [expected_clustered] [cluster_link0]

set -e

HOST=${1:-$(hostname)}
HOST_TYPE=${2:-}
TIMEZONE=${3:-UTC}
IMPORT_POOLS=${4:-}
MOUNTS=${5:-}
EXPECTED_CLUSTERED=${6:-false}
CLUSTER_LINK0=${7:-}
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
            printf '%s\n' proxmox.sources pve-test.sources no-nag-script pve-remove-nag.sh sshd-hardening.conf notify-failure.sh homelab-notify-failure@.service homelab-pve-cluster-rejoin-helper
            ;;
        *)
            return 1
            ;;
    esac
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
    for file in proxmox.sources pve-test.sources; do
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

import_zfs_pools() {
    local pools="$1"
    if [[ -z "$pools" ]]; then
        print_sub "No ZFS pools configured for import; skipping"
        return 0
    fi

    if ! command -v zpool >/dev/null 2>&1; then
        print_warn "zpool not found; skipping pool import"
        return 0
    fi

    for pool in $pools; do
        if zpool list "$pool" >/dev/null 2>&1; then
            print_sub "Pool $pool already imported; skipping"
        else
            print_sub "Importing ZFS pool: $pool"
            zpool import -f "$pool" || print_warn "Failed to import pool $pool"
        fi
    done
}

ensure_local_zfs_storage() {
    local current_node
    local pool

    if ! command -v pvesm >/dev/null 2>&1; then
        print_warn "pvesm not found; skipping zfs storage reconciliation"
        return 0
    fi

    if ! command -v zpool >/dev/null 2>&1; then
        print_warn "zpool not found; skipping zfs storage reconciliation"
        return 0
    fi

    if ! zpool list rpool >/dev/null 2>&1; then
        print_warn "rpool not found; skipping zfs storage reconciliation"
        return 0
    fi

    current_node="$(hostname -s)"
    for pool in vm-disks vm-flash vault-disks vault-hdd; do
        ensure_zfspool_storage "$pool" "$current_node"
    done
}

ensure_zfspool_storage() {
    local pool="$1"
    local current_node="$2"
    local existing_nodes

    if ! zpool list "$pool" >/dev/null 2>&1; then
        return 0
    fi

    if pvesm status --storage "$pool" >/dev/null 2>&1; then
        print_sub "$pool storage already configured"
        existing_nodes="$(storage_nodes "$pool")"
        if [[ -n "$existing_nodes" ]] && ! node_in_csv "$current_node" "$existing_nodes"; then
            print_sub "Adding $current_node to $pool storage node list"
            pvesm set "$pool" --nodes "${existing_nodes},${current_node}" || print_warn "failed to update $pool storage nodes"
        fi
        return 0
    fi

    print_sub "Creating $pool storage on $pool..."
    if [[ -f /etc/pve/corosync.conf ]]; then
        pvesm add zfspool "$pool" --pool "$pool" --content images,rootdir --sparse 0 --nodes "$current_node" || print_warn "failed to create $pool storage"
    else
        pvesm add zfspool "$pool" --pool "$pool" --content images,rootdir --sparse 0 || print_warn "failed to create $pool storage"
    fi
}

storage_nodes() {
    local storage="$1"
    awk -v storage="$storage" '
        $1 == "zfspool:" && $2 == storage { inside = 1; next }
        $0 !~ /^[[:space:]]/ { inside = 0 }
        inside && $1 == "nodes" { print $2; exit }
    ' /etc/pve/storage.cfg 2>/dev/null || true
}

node_in_csv() {
    local needle="$1"
    local csv="$2"
    local old_ifs="$IFS"
    local item
    IFS=','
    for item in $csv; do
        if [[ "$item" == "$needle" ]]; then
            IFS="$old_ifs"
            return 0
        fi
    done
    IFS="$old_ifs"
    return 1
}

ensure_required_packages() {
    local missing_pkgs=()
    local package

    for package in mbuffer vim mc; do
        if ! dpkg -s "$package" >/dev/null 2>&1; then
            missing_pkgs+=("$package")
        fi
    done

    if [[ ${#missing_pkgs[@]} -eq 0 ]]; then
        print_sub "Required packages already installed"
        return 0
    fi

    print_sub "Installing required packages: ${missing_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -q "${missing_pkgs[@]}"
}

configure_native_zfs_scrub_timers() {
    local pool
    local monthly_timer
    local weekly_timer

    if ! command -v zpool >/dev/null 2>&1; then
        print_warn "zpool not found; skipping native scrub timer setup"
        return 0
    fi

    if ! systemctl cat zfs-scrub-monthly@.timer >/dev/null 2>&1; then
        print_warn "native zfs-scrub-monthly@.timer not available; skipping scrub timer setup"
        return 0
    fi

    if systemctl is-enabled --quiet zfs-scrub.timer 2>/dev/null; then
        systemctl disable --now zfs-scrub.timer
        print_ok "zfs-scrub.timer disabled in favor of native PVE scrub timers"
    fi

    while IFS= read -r pool; do
        [[ -n "$pool" ]] || continue

        monthly_timer="zfs-scrub-monthly@${pool}.timer"
        weekly_timer="zfs-scrub-weekly@${pool}.timer"

        if systemctl is-enabled --quiet "$weekly_timer" 2>/dev/null; then
            systemctl disable --now "$weekly_timer"
            print_ok "$weekly_timer disabled"
        fi

        if ! systemctl is-enabled --quiet "$monthly_timer" 2>/dev/null; then
            systemctl enable --now "$monthly_timer"
            print_ok "$monthly_timer enabled"
        else
            print_sub "$monthly_timer already enabled"
        fi
    done < <(zpool list -H -o name)
}

mask_unwanted_default_service() {
    local unit="$1"
    local reason="$2"

    if ! systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        return 0
    fi
    if [[ "$(systemctl is-enabled "$unit" 2>/dev/null)" == "masked" ]]; then
        # Idempotent even if already masked: a masked unit can still show up in
        # `systemctl --failed` from before it was masked (or from any spurious
        # start attempt), and that stale record would otherwise trip a
        # failed-unit alert.
        systemctl reset-failed "$unit" >/dev/null 2>&1 || true
        print_sub "$unit already masked"
        return 0
    fi
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    systemctl mask "$unit"
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    print_ok "$unit masked ($reason)"
}

mask_unwanted_default_services() {
    # openipmi: LSB init script that fails at boot on hardware with no BMC/IPMI
    # device (/dev/ipmi0 absent). None of ace/bray/clovis/osiris have IPMI, so it
    # can never succeed here; mask it rather than leave a permanently-failed unit
    # for any systemd failed-unit alert to trip over.
    mask_unwanted_default_service openipmi.service "no IPMI hardware on this host"
}

install_other_subfeatures() {
    if [[ -f "$BUILD_DIR/interfaces" ]]; then
        print_sub "Configuring network interfaces..."
        bash "$SCRIPT_DIR/scripts/install-interfaces.sh" "$HOST" || return 1
    else
        print_sub "Network interfaces not configured; skipping"
    fi

    if [[ -f "$BUILD_DIR/homelab-site-routes" ]]; then
        print_sub "Configuring site routes..."
        if systemctl list-unit-files homelab-cinci-pikvm-routes.service >/dev/null 2>&1; then
            systemctl disable --now homelab-cinci-pikvm-routes.service >/dev/null 2>&1 || true
            rm -f /etc/systemd/system/homelab-cinci-pikvm-routes.service /usr/local/sbin/homelab-cinci-pikvm-routes
        fi
        install_file homelab-site-routes || return 1
        install_file homelab-site-routes.service || return 1
        systemctl daemon-reload
        systemctl enable --now homelab-site-routes.service >/dev/null
        print_ok "homelab-site-routes.service enabled"
    else
        print_sub "Site routes not configured; skipping"
    fi
}

report_cluster_join_if_needed() {
    local local_node
    local peer_hint
    local peer
    local fingerprint

    [[ "$EXPECTED_CLUSTERED" == "true" ]] || return 0

    if [[ -f /etc/pve/corosync.conf ]] && pvecm status >/dev/null 2>&1; then
        print_sub "Cluster membership detected"
        return 0
    fi

    local_node="$(hostname -s)"
    peer_hint="ace.freender.internal"
    if [[ "$local_node" == "ace" ]]; then
        peer_hint="bray.freender.internal"
    fi

    print_warn "$local_node is expected to be clustered, but currently appears standalone"
    print_sub "Attempting safe cleanup/readiness from cluster peer $peer_hint"
    for peer in "$peer_hint" ace.freender.internal bray.freender.internal clovis.freender.internal; do
        [[ "$peer" != "$local_node.freender.internal" ]] || continue
        if ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$peer" \
            "test -x /usr/local/sbin/homelab-pve-cluster-rejoin-helper && /usr/local/sbin/homelab-pve-cluster-rejoin-helper '$local_node' '$peer'"; then
            print_manual_cluster_join "$peer"
            return 0
        fi
    done

    print_warn "Automatic cluster cleanup failed; run from an existing cluster node:"
    print_sub "homelab-pve-cluster-rejoin-helper $local_node $peer_hint"
    print_manual_cluster_join "$peer_hint"
}

cluster_peer_fingerprint() {
    local peer="$1"

    openssl s_client -connect "$peer:8006" -servername "$peer" </dev/null 2>/dev/null \
        | openssl x509 -noout -fingerprint -sha256 2>/dev/null \
        | cut -d= -f2 || true
}

print_manual_cluster_join() {
    local peer="$1"
    local link0="${CLUSTER_LINK0:-$(hostname -I | awk '{print $1}')}"
    local fingerprint

    if [[ -f /etc/pve/corosync.conf ]]; then
        print_sub "Cluster config appeared after cleanup; manual pvecm add not needed"
        return 0
    fi
    fingerprint="$(cluster_peer_fingerprint "$peer")"

    print_warn "Cluster join is a manual step"
    if [[ -n "$fingerprint" ]]; then
        print_sub "Manual command: pvecm add $peer --fingerprint $fingerprint --link0 $link0"
    else
        print_warn "Cluster peer fingerprint unavailable; verify it manually"
        print_sub "Manual command: pvecm add $peer --link0 $link0"
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
        for file in proxmox.sources pve-test.sources; do
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
        sshd_dest="${FILE_MAP_DEST[sshd-hardening.conf]}"
        sshd_backup=""
        if [[ -f "$sshd_dest" ]]; then
            sshd_backup="$(mktemp)"
            cp "$sshd_dest" "$sshd_backup"
        fi
        if ! install_file sshd-hardening.conf; then
            [[ -n "$sshd_backup" ]] && rm -f "$sshd_backup"
            exit 1
        fi
        if [[ "$INSTALL_FILE_CHANGED" == "true" ]]; then
            if sshd -t 2>/dev/null; then
                systemctl reload sshd && print_sub "sshd reloaded with hardened config"
            else
                # Roll back: warning-only left the bad drop-in on disk, where it would
                # break the next sshd restart or reboot and lock us out of the node.
                if [[ -n "$sshd_backup" ]]; then
                    cp "$sshd_backup" "$sshd_dest"
                else
                    rm -f "$sshd_dest"
                fi
                print_error "sshd -t failed; rolled back $sshd_dest"
                [[ -n "$sshd_backup" ]] && rm -f "$sshd_backup"
                exit 1
            fi
        fi
        [[ -n "$sshd_backup" ]] && rm -f "$sshd_backup"

        print_sub "Deploying failure notification helper..."
        install_file notify-failure.sh || exit 1
        notify_unit_changed=false
        install_file homelab-notify-failure@.service || exit 1
        if [[ "$INSTALL_FILE_CHANGED" == "true" ]]; then
            notify_unit_changed=true
        fi
        if [[ "$notify_unit_changed" == "true" ]]; then
            systemctl daemon-reload
        fi

        print_sub "Deploying cluster rejoin helper..."
        install_file homelab-pve-cluster-rejoin-helper || exit 1

        print_sub "Installing required packages..."
        ensure_required_packages || exit 1

        print_sub "Importing ZFS pools..."
        import_zfs_pools "$IMPORT_POOLS"

        print_sub "Reconciling local ZFS storage..."
        ensure_local_zfs_storage

        print_sub "Configuring native ZFS scrub timers..."
        configure_native_zfs_scrub_timers || exit 1

        print_sub "Masking unwanted default services..."
        mask_unwanted_default_services

        print_sub "Applying additional subfeatures..."
        install_other_subfeatures || exit 1

        print_sub "Configuring disk mounts..."
        bash "$SCRIPT_DIR/scripts/install-mounts.sh" "$MOUNTS" || exit 1

        report_cluster_join_if_needed
        ;;
    *)
        print_warn "Unsupported host type: $HOST_TYPE"
        exit 1
        ;;
esac

print_sub "Configuring local postfix service..."
if systemctl list-unit-files postfix.service >/dev/null 2>&1; then
    if command -v newaliases >/dev/null 2>&1 && [[ -f /etc/aliases ]]; then
        newaliases || print_warn "failed to rebuild postfix aliases"
    fi
    systemctl enable --now postfix || print_warn "failed to enable postfix"
    systemctl reload postfix || print_warn "failed to reload postfix"
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
