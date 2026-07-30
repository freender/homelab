#!/bin/bash
# remove.sh - Remove pve exporters from remote hosts
# Usage: ./remove.sh [--yes] [--purge] <hostname|all>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOMELAB_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=/dev/null
source "$HOMELAB_ROOT/lib/print.sh"

list_feature_hosts() {
    if [[ -x "$HOMELAB_ROOT/.venv/bin/python" ]]; then
        PYTHONPATH="$HOMELAB_ROOT/src" "$HOMELAB_ROOT/.venv/bin/python" -m homelab.cli hosts list --feature "$1"
        return
    fi

    if command -v uv >/dev/null 2>&1; then
        PYTHONPATH="$HOMELAB_ROOT/src" uv run --directory "$HOMELAB_ROOT" python -m homelab.cli hosts list --feature "$1"
        return
    fi

    PYTHONPATH="$HOMELAB_ROOT/src" python3 -m homelab.cli hosts list --feature "$1"
}

filter_hosts() {
    local requested="${1:-all}"
    shift
    local supported=("$@")
    local host

    if [[ "$requested" == "" || "$requested" == "all" ]]; then
        printf '%s\n' "${supported[@]}"
        return 0
    fi

    for host in "${supported[@]}"; do
        if [[ "$host" == "$requested" ]]; then
            printf '%s\n' "$host"
            return 0
        fi
    done

    return 1
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

read -r -a SUPPORTED_HOSTS <<< "$(list_feature_hosts pve-exporters)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-exporters removal (not applicable to $1)"
    exit 0
fi

if [[ "$SKIP_CONFIRM" == "false" ]]; then
    print_header "pve-exporters Removal Plan"
    print_sub "Hosts: $HOSTS"
    print_sub "Actions: stop services, backup and remove smartctl/apcupsd exporter files"
    [[ "$PURGE" == "true" ]] && print_sub "Also purge prometheus-node-exporter package"
    echo ""
    read -p "Proceed with removal? [y/N]: " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 0
fi

for HOST in $HOSTS; do
    if ! ssh "$HOST" "rm -rf /tmp/homelab-pve-exporters-remove && mkdir -p /tmp/homelab-pve-exporters-remove/lib"; then
        print_warn "Failed to stage utils on $HOST"
    else
        scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$HOST:/tmp/homelab-pve-exporters-remove/lib/" || true
    fi
done

FAILED_HOSTS=()
for HOST in $HOSTS; do
    host_failed=false
    print_action "Removing from $HOST..."

    print_sub "Stopping services..."
    # smartctl_exporter is the distro-packaged unit; smartctl-exporter (hyphen)
    # is the retired self-managed one, still stopped here for older hosts.
    ssh "$HOST" "systemctl disable --now smartctl_exporter 2>/dev/null || true" || host_failed=true
    ssh "$HOST" "systemctl disable --now smartctl-exporter 2>/dev/null || true" || host_failed=true
    ssh "$HOST" "systemctl disable --now apcupsd-exporter 2>/dev/null || true" || host_failed=true
    ssh "$HOST" "systemctl disable --now prometheus-node-exporter 2>/dev/null || true" || host_failed=true

    print_sub "Backing up configs..."
    if ! ssh "$HOST" bash <<'EOF'
source /tmp/homelab-pve-exporters-remove/lib/utils.sh
backup_config /etc/systemd/system/smartctl-exporter.service
backup_config /etc/default/smartctl-exporter
backup_config /usr/local/bin/smartctl_exporter
backup_config /etc/systemd/system/apcupsd-exporter.service
backup_config /etc/default/apcupsd-exporter
backup_config /usr/local/bin/apcupsd-exporter
EOF
    then
        host_failed=true
    fi

    print_sub "Removing files..."
    ssh "$HOST" "rm -f /etc/systemd/system/smartctl-exporter.service /etc/default/smartctl-exporter /usr/local/bin/smartctl_exporter /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter" || host_failed=true
    ssh "$HOST" "rm -rf /etc/systemd/system/smartctl_exporter.service.d" || host_failed=true
    ssh "$HOST" "systemctl daemon-reload" || host_failed=true

    if [[ "$PURGE" == "true" ]]; then
        print_sub "Purging packages..."
        ssh "$HOST" "DEBIAN_FRONTEND=noninteractive apt-get purge -y prometheus-node-exporter prometheus-smartctl-exporter >/dev/null 2>&1 || true" || host_failed=true
        ssh "$HOST" "rm -f /etc/apt/sources.list.d/debian-backports.sources" || host_failed=true
    fi

    ssh "$HOST" "rm -rf /tmp/homelab-pve-exporters-remove" >/dev/null 2>&1 || true

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
    print_sub "Failed hosts: ${FAILED_HOSTS[*]}"
    exit 1
fi
