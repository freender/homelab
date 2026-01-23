#!/bin/bash
# remove.sh - Remove telegraf config from remote hosts
# Usage: ./remove.sh [--yes] [--purge] [--remove-repo] [host1 host2 ...] or ./remove.sh all

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOSTS_FILE="$SCRIPT_DIR/hosts.conf"

PURGE=false
REMOVE_REPO=false
SKIP_CONFIRM=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)
            PURGE=true
            shift
            ;;
        --remove-repo)
            REMOVE_REPO=true
            shift
            ;;
        --yes|-y)
            SKIP_CONFIRM=true
            shift
            ;;
        --help|-h)
            cat << 'EOF'
Usage: ./remove.sh [--yes] [--purge] [--remove-repo] <hostname|all>

Options:
  --yes          Skip confirmation prompt
  --purge        Also remove telegraf package
  --remove-repo  Also remove InfluxData apt repository
EOF
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

SUPPORTED_HOSTS=($(hosts list --feature telegraf))

if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping telegraf removal (not applicable to $1)"
    exit 0
fi

if [[ "$SKIP_CONFIRM" == "false" ]]; then
    print_header "telegraf Removal Plan"
    echo "Hosts: $HOSTS"
    echo ""
    echo "Actions per host:"
    echo "  - Stop and disable telegraf service"
    echo "  - Backup /etc/telegraf/ to /etc/telegraf.bak.TIMESTAMP"
    echo "  - Remove config files and sudoers rule"
    if [[ "$REMOVE_REPO" == "true" ]]; then
        echo "  - Remove InfluxData apt repository"
    fi
    if [[ "$PURGE" == "true" ]]; then
        echo "  - Purge telegraf package"
    fi
    echo ""
    read -p "Proceed with removal? [y/N]: " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Cancelled."
        exit 0
    fi
    echo ""
fi

for HOST in $HOSTS; do
    if ! ssh "$HOST" "rm -rf /tmp/homelab-telegraf-remove && mkdir -p /tmp/homelab-telegraf-remove/lib"; then
        print_warn "Failed to stage utils on $HOST"
    else
        scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$HOST:/tmp/homelab-telegraf-remove/lib/" || true
    fi
done

print_header "Removing telegraf"
echo "Hosts: $HOSTS"
echo ""

FAILED_HOSTS=()

for HOST in $HOSTS; do
    host_failed=false

    print_action "Removing from $HOST..."

    if ! ssh "$HOST" "true" >/dev/null 2>&1; then
        print_warn "Failed to connect to $HOST"
        FAILED_HOSTS+=("$HOST")
        echo ""
        continue
    fi

    print_sub "Stopping service..."
    if ! ssh "$HOST" "systemctl stop telegraf 2>/dev/null || true"; then
        host_failed=true
    fi
    if ! ssh "$HOST" "systemctl disable telegraf 2>/dev/null || true"; then
        host_failed=true
    fi

    print_sub "Backing up configs..."
    if ! ssh "$HOST" bash <<'EOF'
source /tmp/homelab-telegraf-remove/lib/utils.sh
backup_config /etc/telegraf
EOF
    then
        host_failed=true
    fi

    print_sub "Removing configs..."
    if ! ssh "$HOST" "rm -f /etc/telegraf/telegraf.conf"; then
        host_failed=true
    fi
    if ! ssh "$HOST" "rm -rf /etc/telegraf/telegraf.d"; then
        host_failed=true
    fi
    if ! ssh "$HOST" "rm -f /etc/sudoers.d/telegraf-smartctl"; then
        host_failed=true
    fi

    if [[ "$REMOVE_REPO" == "true" ]]; then
        print_sub "Removing InfluxData repository..."
        if ! ssh "$HOST" "rm -f /etc/apt/sources.list.d/influxdata.list"; then
            host_failed=true
        fi
        if ! ssh "$HOST" "rm -f /etc/apt/keyrings/influxdata-archive.gpg"; then
            host_failed=true
        fi
        if ! ssh "$HOST" "apt-get update -qq >/dev/null 2>&1 || true"; then
            host_failed=true
        fi
    fi

    if [[ "$PURGE" == "true" ]]; then
        print_sub "Purging package..."
        if ! ssh "$HOST" "DEBIAN_FRONTEND=noninteractive apt-get purge -y telegraf >/dev/null 2>&1 || true"; then
            host_failed=true
        fi
    fi

    ssh "$HOST" "rm -rf /tmp/homelab-telegraf-remove" >/dev/null 2>&1 || true

    if [[ "$host_failed" == "true" ]]; then
        print_warn "Removal completed with errors on $HOST"
        FAILED_HOSTS+=("$HOST")
    else
        print_ok "Removed from $HOST"
    fi

    echo ""
done

print_header "Removal complete"

if [[ ${#FAILED_HOSTS[@]} -gt 0 ]]; then
    echo ""
    echo "Failed hosts: ${FAILED_HOSTS[*]}"
    exit 1
fi
