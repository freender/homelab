#!/bin/bash
# deploy.sh - Deploy apcupsd config to remote hosts via SSH
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILES_DIR="$SCRIPT_DIR/profiles"
RENDERED_DIR="$SCRIPT_DIR/rendered"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

get_apcupsd_hosts() {
    local hosts=()
    local -A seen=()
    local list=(
        $(get_hosts_with_feature ups-master)
        $(get_hosts_with_feature ups-slave)
        $(get_hosts_with_feature ups-standalone)
    )

    for host in "${list[@]}"; do
        if [[ -n "$host" && -z "${seen[$host]:-}" ]]; then
            hosts+=("$host")
            seen[$host]=1
        fi
    done

    printf '%s\n' "${hosts[@]}"
}

get_slave_hosts() {
    get_hosts_with_feature ups-slave
}

load_host_config() {
    local host="$1"

    ROLE=$(get_host_kv "$host" "ups.role")
    UPSNAME=$(get_host_kv "$host" "ups.name")
    DEVICE=$(get_host_kv "$host" "ups.device")
    NISIP=$(get_host_kv "$host" "ups.nisip")

    if [[ -z "$ROLE" || -z "$UPSNAME" || -z "$NISIP" ]]; then
        echo "ERROR: Missing ups.* config for $host in apcupsd/hosts.conf"
        return 1
    fi
}

# Get supported hosts from registry features
SUPPORTED_HOSTS=($(get_apcupsd_hosts))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apcupsd (not applicable to $1)"
    exit 0
fi

# Validate shared telegram.env exists
TELEGRAM_ENV="${SCRIPT_DIR}/telegram/telegram.env"
if [[ ! -f "$TELEGRAM_ENV" ]]; then
    echo "ERROR: telegram.env not found!"
    echo ""
    echo "Create it from the example:"
    echo "  cp apcupsd/telegram/telegram.env.example apcupsd/telegram/telegram.env"
    echo "  # Edit with your actual TELEGRAM_TOKEN and TELEGRAM_CHATID"
    exit 1
fi

# Render templates
render_configs() {
    local host="$1"
    local role="$2"
    local upsname="$3"
    local device="$4"
    local nisip="$5"
    local slave_hosts="$6"

    local host_dir="$RENDERED_DIR/$host"
    mkdir -p "$host_dir"

    local conf_template=""
    local shutdown_template=""

    case "$role" in
        master)
            conf_template="$PROFILES_DIR/master.conf.tpl"
            shutdown_template="$PROFILES_DIR/doshutdown-master.tpl"
            ;;
        slave)
            conf_template="$PROFILES_DIR/slave.conf.tpl"
            shutdown_template="$PROFILES_DIR/doshutdown-slave.tpl"
            ;;
        master-standalone)
            conf_template="$PROFILES_DIR/master.conf.tpl"
            shutdown_template="$PROFILES_DIR/doshutdown-master-standalone.tpl"
            ;;
        *)
            echo "ERROR: Unknown role '$role' for host $host"
            return 1
            ;;
    esac

    for template in "$conf_template" "$shutdown_template"; do
        if [[ ! -f "$template" ]]; then
            echo "ERROR: Missing template $template"
            return 1
        fi
    done

    local conf_out="$host_dir/apcupsd.conf"
    local shutdown_out="$host_dir/doshutdown"

    render_template "$conf_template" "$conf_out" "$host" "$upsname" "$device" "$nisip" "$slave_hosts"
    render_template "$shutdown_template" "$shutdown_out" "$host" "$upsname" "$device" "$nisip" "$slave_hosts"
    chmod +x "$shutdown_out"
}

render_template() {
    local template="$1"
    local output="$2"
    local host="$3"
    local upsname="$4"
    local device="$5"
    local nisip="$6"
    local slave_hosts="$7"

    local content
    content=$(cat "$template")
    content=${content//\$\{HOST\}/$host}
    content=${content//\$\{UPSNAME\}/$upsname}
    content=${content//\$\{DEVICE\}/$device}
    content=${content//\$\{NISIP\}/$nisip}
    content=${content//\$\{SLAVE_HOSTS\}/$slave_hosts}

    printf '%s\n' "$content" > "$output"
}

print_header "Deploying apcupsd"
echo "Hosts: $HOSTS"
echo ""

SLAVE_HOSTS=$(get_slave_hosts)

for HOST in $HOSTS; do
    print_header "Deploying apcupsd to $HOST"

    if ! load_host_config "$HOST"; then
        continue
    fi

    render_configs "$HOST" "$ROLE" "$UPSNAME" "$DEVICE" "$NISIP" "$SLAVE_HOSTS"

    # Create temp directory on target and copy files
    print_sub "Copying files to $HOST:/tmp/homelab-apcupsd/..."
    ssh "$HOST" "rm -rf /tmp/homelab-apcupsd && mkdir -p /tmp/homelab-apcupsd"

    # Copy rendered configs and scripts
    scp -rq "$RENDERED_DIR/$HOST" "$SCRIPT_DIR/scripts" "$SCRIPT_DIR/shared" "$SCRIPT_DIR/hosts.conf" "$HOST:/tmp/homelab-apcupsd/"

    # Copy shared telegram files (from homelab root shared/)
    ssh "$HOST" "mkdir -p /tmp/homelab-apcupsd/telegram"
    scp -rq "$SCRIPT_DIR/telegram/"* "$HOST:/tmp/homelab-apcupsd/telegram/"

    # Run installer on target
    print_sub "Running installer on $HOST..."
    ssh "$HOST" "cd /tmp/homelab-apcupsd && chmod +x scripts/install.sh && ./scripts/install.sh $HOST"

    echo ""
done

print_header "Deployment complete"
