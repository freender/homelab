#!/bin/bash
# Deploy PVE/PBS post-install configs
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
PVE_CONFIG_DIR="$SCRIPT_DIR/configs/pve"
PBS_CONFIG_DIR="$SCRIPT_DIR/configs/pbs"

PVE_FILES=(
    proxmox.sources
    pve-enterprise.sources
    ceph.sources
    pve-test.sources
    no-nag-script
    pve-remove-nag.sh
    pve-ceph-reconcile.sh
)

PBS_FILES=(
    proxmox.sources
    pbs-enterprise.sources
    no-nag-script
    pbs-remove-nag.sh
)

remote_path_for_file() {
    local file="$1"
    case "$file" in
        proxmox.sources|pve-enterprise.sources|ceph.sources|pve-test.sources|pbs-enterprise.sources)
            echo "/etc/apt/sources.list.d/$file"
            ;;
        no-nag-script)
            echo "/etc/apt/apt.conf.d/no-nag-script"
            ;;
        pve-remove-nag.sh|pbs-remove-nag.sh)
            echo "/usr/local/bin/$file"
            ;;
        pve-ceph-reconcile.sh)
            echo "/usr/local/sbin/$file"
            ;;
        *)
            return 1
            ;;
    esac
}

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
    local timezone
    local config_dir
    local -a files
    local build_dir="$BUILD_ROOT/$host"

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    timezone=$(hosts get "$host" "pve-postinstall.timezone" "UTC")
    case "$host_type" in
        pve)
            config_dir="$PVE_CONFIG_DIR"
            files=("${PVE_FILES[@]}")
            ;;
        pbs)
            config_dir="$PBS_CONFIG_DIR"
            files=("${PBS_FILES[@]}")
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

    print_sub "Comparing with remote configs..."
    for file in "${files[@]}"; do
        local remote_path
        remote_path=$(remote_path_for_file "$file") || { print_warn "No remote path mapping for $file"; return 1; }
        diff_remote_config "$host" "$build_dir/$file" "$remote_path" || true
    done

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
    ssh "$host" "cd /tmp/homelab-pve-postinstall && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host $host_type '$timezone'"
}

deploy_init "PVE/PBS Post-Install Configs"
deploy_run deploy $HOSTS
deploy_finish
