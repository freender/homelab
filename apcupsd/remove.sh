#!/bin/bash
# remove.sh - Remove apcupsd config from remote hosts
# Usage: ./remove.sh [--yes] [--purge] [host1 host2 ...] or ./remove.sh all

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

get_apcupsd_hosts() {
    local hosts=()
    local -A seen=()
    local list=(
        $(hosts list --feature ups-master)
        $(hosts list --feature ups-slave)
        $(hosts list --feature ups-standalone)
    )

    for host in "${list[@]}"; do
        if [[ -n "$host" && -z "${seen[$host]:-}" ]]; then
            hosts+=("$host")
            seen[$host]=1
        fi
    done

    printf '%s\n' "${hosts[@]}"
}

PURGE=false
SKIP_CONFIRM=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)
            PURGE=true
            shift
            ;;
        --yes|-y)
            SKIP_CONFIRM=true
            shift
            ;;
        --help|-h)
            cat << 'EOF'
Usage: ./remove.sh [--yes] [--purge] <hostname|all>

Options:
  --yes       Skip confirmation prompt
  --purge     Also remove apcupsd package
EOF
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

SUPPORTED_HOSTS=($(get_apcupsd_hosts))

if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping apcupsd removal (not applicable to $1)"
    exit 0
fi

if [[ "$SKIP_CONFIRM" == "false" ]]; then
    print_header "apcupsd Removal Plan"
    echo "Hosts: $HOSTS"
    echo ""
    echo "Actions per host:"
    echo "  - Stop and disable apcupsd service"
    echo "  - Backup /etc/apcupsd/ to /etc/apcupsd.bak.TIMESTAMP"
    echo "  - Remove config files and telegram integration"
    echo "  - Reset /etc/default/apcupsd (ISCONFIGURED=no)"
    if [[ "$PURGE" == "true" ]]; then
        echo "  - Purge apcupsd package"
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
    if ! ssh "$HOST" "rm -rf /tmp/homelab-apcupsd-remove && mkdir -p /tmp/homelab-apcupsd-remove/lib"; then
        print_warn "Failed to stage utils on $HOST"
    else
        scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$HOST:/tmp/homelab-apcupsd-remove/lib/" || true
    fi
done

print_header "Removing apcupsd"
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
    if ! ssh "$HOST" "systemctl stop apcupsd 2>/dev/null || true"; then
        host_failed=true
    fi
    if ! ssh "$HOST" "systemctl disable apcupsd 2>/dev/null || true"; then
        host_failed=true
    fi

    print_sub "Backing up configs..."
    if ! ssh "$HOST" bash <<'EOF'
source /tmp/homelab-apcupsd-remove/lib/utils.sh
backup_config /etc/apcupsd
EOF
    then
        host_failed=true
    fi

    print_sub "Removing configs..."
    if ! ssh "$HOST" "rm -f /etc/apcupsd/apcupsd.conf /etc/apcupsd/doshutdown /etc/apcupsd/apcupsd.notify"; then
        host_failed=true
    fi
    if ! ssh "$HOST" "rm -rf /etc/apcupsd/telegram"; then
        host_failed=true
    fi

    print_sub "Resetting default config..."
    if ! ssh "$HOST" "if [ -f /etc/default/apcupsd ]; then sed -i 's/^ISCONFIGURED=yes/ISCONFIGURED=no/' /etc/default/apcupsd; fi"; then
        host_failed=true
    fi

    if [[ "$PURGE" == "true" ]]; then
        print_sub "Purging package..."
        if ! ssh "$HOST" "DEBIAN_FRONTEND=noninteractive apt-get purge -y apcupsd >/dev/null 2>&1 || true"; then
            host_failed=true
        fi
    fi

    ssh "$HOST" "rm -rf /tmp/homelab-apcupsd-remove" >/dev/null 2>&1 || true

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
