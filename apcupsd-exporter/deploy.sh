#!/bin/bash

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$SCRIPT_DIR/configs"
COMMON_DIR="$CONFIGS_DIR/common"
BUILD_ROOT="$SCRIPT_DIR/build"

get_apcupsd_exporter_hosts() {
    local host role selected=()
    read -r -a hosts_with_apcupsd <<< "$(hosts list --feature apcupsd)"
    for host in "${hosts_with_apcupsd[@]}"; do
        role=$(hosts get "$host" "apcupsd.role" "none")
        if [[ "$role" == "master" || "$role" == "master-standalone" ]]; then
            selected+=("$host")
        fi
    done
    echo "${selected[*]}"
}

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(get_apcupsd_exporter_hosts)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apcupsd-exporter (not applicable to $1)"
    exit 0
fi

validate() {
    local required=(apcupsd-exporter.env apcupsd-exporter.service apcupsd-exporter.py)
    [[ ! -d "$COMMON_DIR" ]] && { echo "Error: $COMMON_DIR not found"; return 1; }
    for conf in "${required[@]}"; do
        [[ ! -f "$COMMON_DIR/$conf" ]] && { echo "Error: Missing $COMMON_DIR/$conf"; return 1; }
    done
    return 0
}
validate || exit 1

deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"
    local upsname serial

    upsname=$(hosts get "$host" "apcupsd.name") || { print_warn "apcupsd.name missing"; return 1; }
    serial=$(ssh "$host" "apcaccess status 2>/dev/null | sed -n 's/^SERIALNO[[:space:]]*:[[:space:]]*//p' | xargs" 2>/dev/null || true)

    prepare_build_dir "$build_dir"
    mkdir -p "$build_dir/configs"

    cp "$COMMON_DIR/apcupsd-exporter.py" "$build_dir/configs/apcupsd-exporter.py"
    cp "$COMMON_DIR/apcupsd-exporter.service" "$build_dir/configs/apcupsd-exporter.service"
    render_template "$COMMON_DIR/apcupsd-exporter.env" "$build_dir/configs/apcupsd-exporter.env" \
        UPS_NAME="$upsname" UPS_HOST="$host" UPS_SERIAL="$serial"

    diff_remote_config "$host" "$build_dir/configs/apcupsd-exporter.py" "/usr/local/bin/apcupsd-exporter" || true
    diff_remote_config "$host" "$build_dir/configs/apcupsd-exporter.service" "/etc/systemd/system/apcupsd-exporter.service" || true
    diff_remote_config "$host" "$build_dir/configs/apcupsd-exporter.env" "/etc/default/apcupsd-exporter" || true

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-apcupsd-exporter/"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-apcupsd-exporter && mkdir -p /tmp/homelab-apcupsd-exporter/build /tmp/homelab-apcupsd-exporter/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-apcupsd-exporter/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-apcupsd-exporter/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-apcupsd-exporter/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-apcupsd-exporter && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo Error: PVE deploy requires root SSH user >&2; exit 1; fi && FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh '$host'"
}

deploy_init "apcupsd exporter"
deploy_run deploy $HOSTS
deploy_finish
