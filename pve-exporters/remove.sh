#!/bin/bash
# remove.sh - Remove pve exporters from remote hosts
# Usage: ./remove.sh [--yes] [--purge] <hostname|all>

source "$(dirname "$0")/../lib/common.sh"

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
            cat << USAGE
Usage: ./remove.sh [--yes] [--purge] <hostname|all>

Options:
  --yes     Skip confirmation prompt
  --purge   Also purge node exporter package
USAGE
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-exporters)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-exporters removal (not applicable to $1)"
    exit 0
fi

if [[ "$SKIP_CONFIRM" == "false" ]]; then
    print_header "pve-exporters Removal Plan"
    echo "Hosts: $HOSTS"
    echo "Actions: stop services, remove smartctl/apcupsd exporter files"
    [[ "$PURGE" == "true" ]] && echo "Also purge prometheus-node-exporter package"
    echo ""
    read -p "Proceed with removal? [y/N]: " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 0
fi

FAILED_HOSTS=()
for HOST in $HOSTS; do
    host_failed=false
    print_action "Removing from $HOST..."

    ssh "$HOST" "systemctl disable --now smartctl-exporter 2>/dev/null || true" || host_failed=true
    ssh "$HOST" "systemctl disable --now apcupsd-exporter 2>/dev/null || true" || host_failed=true
    ssh "$HOST" "systemctl disable --now prometheus-node-exporter 2>/dev/null || true" || host_failed=true
    ssh "$HOST" "rm -f /etc/systemd/system/smartctl-exporter.service /etc/default/smartctl-exporter /usr/local/bin/smartctl_exporter /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter" || host_failed=true
    ssh "$HOST" "systemctl daemon-reload" || host_failed=true

    if [[ "$PURGE" == "true" ]]; then
        ssh "$HOST" "DEBIAN_FRONTEND=noninteractive apt-get purge -y prometheus-node-exporter >/dev/null 2>&1 || true" || host_failed=true
    fi

    if [[ "$host_failed" == "true" ]]; then
        print_warn "Removal completed with errors on $HOST"
        FAILED_HOSTS+=("$HOST")
    else
        print_ok "Removed from $HOST"
    fi
    echo ""
done

if [[ ${#FAILED_HOSTS[@]} -gt 0 ]]; then
    echo "Failed hosts: ${FAILED_HOSTS[*]}"
    exit 1
fi
