#!/bin/bash
# Deploy PVE/PBS post-install configs
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
PVE_CONFIG_DIR="$SCRIPT_DIR/configs/pve"
PBS_CONFIG_DIR="$SCRIPT_DIR/configs/pbs"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-postinstall)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-postinstall (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type
    local config_dir
    local build_dir="$BUILD_ROOT/$host"

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    case "$host_type" in
        pve)
            config_dir="$PVE_CONFIG_DIR"
            ;;
        pbs)
            config_dir="$PBS_CONFIG_DIR"
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

    cp "$config_dir"/* "$build_dir/"

    print_sub "Comparing with remote configs..."
    if [[ "$host_type" == "pve" ]]; then
        diff_remote_config "$host" "$build_dir/proxmox.sources" "/etc/apt/sources.list.d/proxmox.sources" || true
        diff_remote_config "$host" "$build_dir/pve-enterprise.sources" "/etc/apt/sources.list.d/pve-enterprise.sources" || true
        diff_remote_config "$host" "$build_dir/ceph.sources" "/etc/apt/sources.list.d/ceph.sources" || true
        diff_remote_config "$host" "$build_dir/pve-test.sources" "/etc/apt/sources.list.d/pve-test.sources" || true
        diff_remote_config "$host" "$build_dir/no-nag-script" "/etc/apt/apt.conf.d/no-nag-script" || true
        diff_remote_config "$host" "$build_dir/pve-remove-nag.sh" "/usr/local/bin/pve-remove-nag.sh" || true
    else
        diff_remote_config "$host" "$build_dir/proxmox.sources" "/etc/apt/sources.list.d/proxmox.sources" || true
        diff_remote_config "$host" "$build_dir/pbs-enterprise.sources" "/etc/apt/sources.list.d/pbs-enterprise.sources" || true
        diff_remote_config "$host" "$build_dir/no-nag-script" "/etc/apt/apt.conf.d/no-nag-script" || true
    fi

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-postinstall/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-postinstall && mkdir -p /tmp/homelab-pve-postinstall/build /tmp/homelab-pve-postinstall/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-postinstall/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-postinstall/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-postinstall/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-postinstall && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host $host_type"
}

deploy_init "PVE/PBS Post-Install Configs"
deploy_run deploy $HOSTS
deploy_finish
