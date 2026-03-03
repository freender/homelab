#!/bin/bash
# Deploy apcupsd config to remote hosts via SSH
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
CONFIGS_DIR="$SCRIPT_DIR/configs"
BUILD_ROOT="$SCRIPT_DIR/build"
TELEGRAM_ENV="$HOMELAB_ROOT/secrets/telegram.env"

# --- Host Selection ---
get_apcupsd_hosts() {
    hosts list --feature apcupsd
}

# Parse flags
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(get_apcupsd_hosts)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apcupsd (not applicable to $1)"
    exit 0
fi


# --- Validation ---
[[ ! -f "$TELEGRAM_ENV" ]] && {
    echo "ERROR: telegram.env not found!"
    echo "  cp secrets/telegram.env.example secrets/telegram.env"
    exit 1
}

# --- Config Rendering ---
render_configs() {
    local host="$1" role="$2" upsname="$3" device="$4" nisip="$5" slave_hosts="$6"
    local host_dir="$BUILD_ROOT/$host"

    local conf_template shutdown_template
    case "$role" in
        master)
            conf_template="$TEMPLATES_DIR/master.conf.tpl"
            shutdown_template="$TEMPLATES_DIR/doshutdown-master.tpl"
            ;;
        slave)
            conf_template="$TEMPLATES_DIR/slave.conf.tpl"
            shutdown_template="$TEMPLATES_DIR/doshutdown-slave.tpl"
            ;;
        master-standalone)
            conf_template="$TEMPLATES_DIR/master.conf.tpl"
            shutdown_template="$TEMPLATES_DIR/doshutdown-master-standalone.tpl"
            ;;
        *)
            echo "ERROR: Unknown role '$role'"
            return 1
            ;;
    esac

    for tpl in "$conf_template" "$shutdown_template"; do
        [[ ! -f "$tpl" ]] && { echo "ERROR: Missing template $tpl"; return 1; }
    done

    prepare_build_dir "$host_dir"

    render_template "$conf_template" "$host_dir/apcupsd.conf" \
        HOST="$host" UPSNAME="$upsname" DEVICE="$device" NISIP="$nisip" SLAVE_HOSTS="$slave_hosts"
    
    render_template "$shutdown_template" "$host_dir/doshutdown" \
        HOST="$host" UPSNAME="$upsname" DEVICE="$device" NISIP="$nisip" SLAVE_HOSTS="$slave_hosts"
    
    chmod +x "$host_dir/doshutdown"
    cp "$TELEGRAM_ENV" "$host_dir/telegram.env"

    cat > "$host_dir/env" <<EOF
ROLE="$role"
HOST="$host"
EOF
}

# --- Per-Host Deployment ---
get_slave_hosts() {
    local slaves=""
    for h in $(hosts list --feature apcupsd); do
        if [[ "$(hosts get "$h" "apcupsd.role")" == "slave" ]]; then
            slaves="$slaves $h"
        fi
    done
    echo "$slaves"
}
SLAVE_HOSTS=$(get_slave_hosts)

deploy() {
    local host="$1"
    local role upsname device nisip

    role=$(hosts get "$host" "apcupsd.role") || { print_warn "apcupsd.role missing"; return 1; }
    upsname=$(hosts get "$host" "apcupsd.name") || { print_warn "apcupsd.name missing"; return 1; }
    device=$(hosts get "$host" "apcupsd.device" "")
    nisip=$(hosts get "$host" "apcupsd.nisip") || { print_warn "apcupsd.nisip missing"; return 1; }

    render_configs "$host" "$role" "$upsname" "$device" "$nisip" "$SLAVE_HOSTS" || return 1

    print_sub "Comparing with remote configs..."
    diff_remote_config "$host" "$BUILD_ROOT/$host/apcupsd.conf" "/etc/apcupsd/apcupsd.conf" || true
    diff_remote_config "$host" "$BUILD_ROOT/$host/doshutdown" "/etc/apcupsd/doshutdown" || true
    diff_remote_config "$host" "$CONFIGS_DIR/shared/apcupsd.notify" "/etc/apcupsd/apcupsd.notify" || true
    diff_remote_config "$host" "$CONFIGS_DIR/telegram/telegram.sh" "/etc/apcupsd/telegram/telegram.sh" || true
    diff_remote_config "$host" "$TELEGRAM_ENV" "/etc/apcupsd/telegram/telegram.env" || true

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-apcupsd/"
        print_sub "Build files:"
        find "$BUILD_ROOT/$host" -type f | sed "s|$BUILD_ROOT/$host/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-apcupsd && mkdir -p /tmp/homelab-apcupsd/build /tmp/homelab-apcupsd/lib"
    scp -rq "$BUILD_ROOT/$host" "$host:/tmp/homelab-apcupsd/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$CONFIGS_DIR" "$host:/tmp/homelab-apcupsd/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-apcupsd/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-apcupsd && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE/PBS deploy requires root SSH user' >&2; exit 1; fi && FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh '$host'"
}

# --- Main ---
deploy_init "apcupsd"
deploy_run deploy $HOSTS
deploy_finish
