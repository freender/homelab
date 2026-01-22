#!/bin/bash
# Deploy apcupsd config to remote hosts via SSH
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
CONFIGS_DIR="$SCRIPT_DIR/configs"
BUILD_ROOT="$SCRIPT_DIR/build"
TELEGRAM_ENV="$CONFIGS_DIR/telegram/telegram.env"

# --- Host Selection ---
get_apcupsd_hosts() {
    local hosts=() seen=()
    local list=(
        $(hosts list --feature ups-master)
        $(hosts list --feature ups-slave)
        $(hosts list --feature ups-standalone)
    )
    for host in "${list[@]}"; do
        if [[ -n "$host" && ! " ${seen[*]} " =~ " $host " ]]; then
            hosts+=("$host")
            seen+=("$host")
        fi
    done
    printf '%s\n' "${hosts[@]}"
}

SUPPORTED_HOSTS=($(get_apcupsd_hosts))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apcupsd (not applicable to $1)"
    exit 0
fi

# --- Validation ---
[[ ! -f "$TELEGRAM_ENV" ]] && {
    echo "ERROR: telegram.env not found!"
    echo "  cp apcupsd/configs/telegram/telegram.env.example apcupsd/configs/telegram/telegram.env"
    exit 1
}

# --- Template Rendering ---
render_template() {
    local template="$1" output="$2" host="$3" upsname="$4" device="$5" nisip="$6" slave_hosts="$7"
    local content
    content=$(cat "$template")
    content=${content//\$\{HOST\}/$host}
    content=${content//\$\{UPSNAME\}/$upsname}
    content=${content//\$\{DEVICE\}/$device}
    content=${content//\$\{NISIP\}/$nisip}
    content=${content//\$\{SLAVE_HOSTS\}/$slave_hosts}
    printf '%s\n' "$content" > "$output"
}

render_configs() {
    local host="$1" role="$2" upsname="$3" device="$4" nisip="$5" slave_hosts="$6"
    local host_dir="$BUILD_ROOT/$host"
    mkdir -p "$host_dir"

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

    render_template "$conf_template" "$host_dir/apcupsd.conf" "$host" "$upsname" "$device" "$nisip" "$slave_hosts"
    render_template "$shutdown_template" "$host_dir/doshutdown" "$host" "$upsname" "$device" "$nisip" "$slave_hosts"
    chmod +x "$host_dir/doshutdown"

    cat > "$host_dir/env" <<EOF
ROLE="$role"
HOST="$host"
EOF
}

# --- Per-Host Deployment ---
SLAVE_HOSTS=$(hosts list --feature ups-slave)

deploy() {
    local host="$1"
    local role upsname device nisip

    role=$(hosts get "$host" "ups.role") || { print_warn "ups.role missing"; return 1; }
    upsname=$(hosts get "$host" "ups.name") || { print_warn "ups.name missing"; return 1; }
    device=$(hosts get "$host" "ups.device" "")
    nisip=$(hosts get "$host" "ups.nisip") || { print_warn "ups.nisip missing"; return 1; }

    render_configs "$host" "$role" "$upsname" "$device" "$nisip" "$SLAVE_HOSTS" || return 1

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-apcupsd && mkdir -p /tmp/homelab-apcupsd/build"
    scp -rq "$BUILD_ROOT/$host" "$host:/tmp/homelab-apcupsd/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$CONFIGS_DIR" "$host:/tmp/homelab-apcupsd/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-apcupsd && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "apcupsd"
deploy_run deploy $HOSTS
deploy_finish
