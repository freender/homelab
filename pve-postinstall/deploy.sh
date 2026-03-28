#!/bin/bash
# Deploy PVE post-install configs
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
PVE_CONFIG_DIR="$SCRIPT_DIR/configs/pve"
HOSTS_FILE="$HOMELAB_ROOT/hosts.conf"
INTERFACES_TEMPLATE="$SCRIPT_DIR/templates/pve-interfaces"

PVE_FILES=(
    proxmox.sources
    ceph.sources
    pve-test.sources
    no-nag-script
    pve-remove-nag.sh
    sshd-hardening.conf
)

declare -A FILE_REMOTE_PATHS=(
    [proxmox.sources]="/etc/apt/sources.list.d/proxmox.sources"
    [ceph.sources]="/etc/apt/sources.list.d/ceph.sources"
    [pve-test.sources]="/etc/apt/sources.list.d/pve-test.sources"
    [no-nag-script]="/etc/apt/apt.conf.d/no-nag-script"
    [pve-remove-nag.sh]="/usr/local/bin/pve-remove-nag.sh"
    [sshd-hardening.conf]="/etc/ssh/sshd_config.d/99-disable-password-auth.conf"
)
declare -A FILE_MODES=(
    [no-nag-script]="644"
    [pve-remove-nag.sh]="755"
)

write_file_map() {
    local build_dir="$1"
    local file

    for file in "${!FILE_REMOTE_PATHS[@]}"; do
        printf '%s|%s|%s\n' "$file" "${FILE_REMOTE_PATHS[$file]}" "${FILE_MODES[$file]:-644}"
    done > "$build_dir/file-map.conf"
}

remote_path_for_file() {
    local file="$1"

    if [[ -n "${FILE_REMOTE_PATHS[$file]+x}" ]]; then
        echo "${FILE_REMOTE_PATHS[$file]}"
    else
        return 1
    fi
}

build_network_interfaces_bundle() {
    local host="$1"
    local build_dir="$2"
    local interfaces_config
    local mgmt_ip
    local storage_ip
    local gateway

    interfaces_config=$(yq e ".\"$host\".features.pve-postinstall.interfaces // \"\"" "$HOSTS_FILE")
    if [[ -z "$interfaces_config" || "$interfaces_config" == "null" ]]; then
        return 0
    fi

    if [[ ! -f "$INTERFACES_TEMPLATE" ]]; then
        print_warn "Template not found: $INTERFACES_TEMPLATE"
        return 1
    fi

    mgmt_ip=$(yq e ".\"$host\".features.pve-postinstall.interfaces.mgmt_ip // \"\"" "$HOSTS_FILE")
    gateway=$(yq e ".\"$host\".features.pve-postinstall.interfaces.gateway // \"\"" "$HOSTS_FILE")
    storage_ip=$(yq e ".\"$host\".features.pve-postinstall.interfaces.storage_ip // \"\"" "$HOSTS_FILE")

    if [[ -z "$mgmt_ip" || -z "$gateway" || -z "$storage_ip" ]]; then
        print_warn "pve-postinstall.interfaces.{mgmt_ip,gateway,storage_ip} required for $host"
        return 1
    fi

    render_template "$INTERFACES_TEMPLATE" "$build_dir/interfaces" \
        NET_MGMT_IP="$mgmt_ip" \
        NET_GATEWAY="$gateway" \
        NET_STORAGE_IP="$storage_ip"
}

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-postinstall)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-postinstall (not applicable to $1)"
    exit 0
fi

validate() {
    local errors=0
    local file

    for file in "${PVE_FILES[@]}"; do
        if [[ ! -f "$PVE_CONFIG_DIR/$file" ]]; then
            print_error "missing config file: $PVE_CONFIG_DIR/$file"
            errors=$((errors + 1))
        fi
    done

    if [[ ! -f "$INTERFACES_TEMPLATE" ]]; then
        print_error "missing interfaces template: $INTERFACES_TEMPLATE"
        errors=$((errors + 1))
    fi

    if [[ $errors -gt 0 ]]; then
        print_error "validation failed with $errors error(s); aborting"
        exit 1
    fi
}

deploy() {
    local host="$1"
    local host_type
    local timezone
    local ceph_enabled="false"
    local config_dir
    local -a files
    local build_dir="$BUILD_ROOT/$host"
    local file
    local remote_path

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    timezone=$(hosts get "$host" "pve-postinstall.timezone" "UTC")
    case "$host_type" in
        pve)
            config_dir="$PVE_CONFIG_DIR"
            files=("${PVE_FILES[@]}")
            if hosts has "$host" "ceph"; then
                ceph_enabled="true"
            fi
            ;;
        *)
            print_warn "Unsupported host type for $host: $host_type"
            return 1
            ;;
    esac

    if [[ ! -d "$config_dir" ]]; then
        print_warn "Config directory missing: $config_dir"
        return 1
    fi

    prepare_build_dir "$build_dir"

    for file in "${files[@]}"; do
        if [[ ! -f "$config_dir/$file" ]]; then
            print_warn "Missing config file: $config_dir/$file"
            return 1
        fi
        cp "$config_dir/$file" "$build_dir/$file"
    done

    write_file_map "$build_dir"

    if ! build_network_interfaces_bundle "$host" "$build_dir"; then
        return 1
    fi

    print_sub "Comparing with remote configs..."
    for file in "${files[@]}"; do
        remote_path=$(remote_path_for_file "$file") || { print_warn "No remote path mapping for $file"; return 1; }
        diff_remote_config "$host" "$build_dir/$file" "$remote_path" || true
    done

    if [[ -f "$build_dir/interfaces" ]]; then
        diff_remote_config "$host" "$build_dir/interfaces" "/etc/network/interfaces" || true
    fi

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-postinstall/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"

        if [[ -f "$build_dir/interfaces" ]]; then
            print_sub "Network interfaces subfeature: enabled"
        else
            print_sub "Network interfaces subfeature: disabled"
        fi
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-postinstall && mkdir -p /tmp/homelab-pve-postinstall/build /tmp/homelab-pve-postinstall/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-postinstall/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-postinstall/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-postinstall/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-postinstall && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE deploy requires root SSH user' >&2; exit 1; fi && FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh '$host' '$host_type' '$timezone' '$ceph_enabled'"
}

validate
deploy_init "PVE Post-Install Configs"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Apply changes:"
echo "  ssh <node> ifreload -a   # Apply without reboot (may disrupt connections)"
echo "  ssh <node> reboot        # Or reboot to apply safely"
