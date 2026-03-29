#!/bin/bash
# remove.sh - Remove GPU passthrough configuration from PVE nodes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DRY_RUN=false
SKIP_CONFIRM=false
HOSTS_CMD=()

print_header() { printf '=== %s ===\n' "$*"; }
print_action() { printf '==> %s\n' "$*"; }
print_sub() { printf '    %s\n' "$*"; }
print_warn() { printf '    Warning: %s\n' "$*" >&2; }
print_error() { printf 'ERROR: %s\n' "$*" >&2; }

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    HOSTS_CMD=("$REPO_ROOT/.venv/bin/python" -m homelab.cli)
elif command -v uv >/dev/null 2>&1; then
    HOSTS_CMD=(uv run python -m homelab.cli)
else
    HOSTS_CMD=(python3 -m homelab.cli)
fi

show_help() {
    cat <<'EOF'
Usage: ./remove.sh [--yes] [--dry-run] <hostname|all>

Options:
  --yes, -y       Skip confirmation prompt
  --dry-run, -n   Preview changes without executing
  --help, -h      Show this help

For local emergency removal on a Proxmox host:
  /root/pve-gpu-passthrough-remove.sh
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)
            SKIP_CONFIRM=true
            shift
            ;;
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

TARGET_HOST="${1:-}"
if [[ -z "$TARGET_HOST" ]]; then
    print_error "No hostname specified"
    exit 1
fi

SUPPORTED_HOSTS=$(PYTHONPATH="$REPO_ROOT/src" "${HOSTS_CMD[@]}" hosts list --feature pve-gpu-passthrough 2>/dev/null || true)
if [[ -z "$SUPPORTED_HOSTS" ]]; then
    print_error "Unable to query supported hosts"
    print_error "Use repo .venv or install deps first: uv sync"
    exit 1
fi

HOSTS=()
if [[ "$TARGET_HOST" == "all" ]]; then
    while IFS= read -r host; do
        [[ -n "$host" ]] && HOSTS+=("$host")
    done <<< "$SUPPORTED_HOSTS"
else
    while IFS= read -r host; do
        if [[ "$host" == "$TARGET_HOST" ]]; then
            HOSTS+=("$host")
            break
        fi
    done <<< "$SUPPORTED_HOSTS"
fi

if [[ ${#HOSTS[@]} -eq 0 ]]; then
    print_error "Host not supported: $TARGET_HOST"
    print_error "Supported hosts: ${SUPPORTED_HOSTS//$'\n'/ }"
    exit 1
fi

if [[ "$SKIP_CONFIRM" == "false" && "$DRY_RUN" == "false" ]]; then
    print_header "GPU Passthrough Removal Plan"
    printf 'Hosts: %s\n\n' "${HOSTS[*]}"
    printf 'Actions per host:\n'
    printf '  - Remove video=efifb:off from kernel cmdline\n'
    printf '  - Comment out GPU driver blacklists\n'
    printf '  - Comment out VFIO device bindings\n'
    printf '  - Remove VFIO module config\n'
    printf '  - Update initramfs and bootloader\n\n'
    read -r -p 'Proceed with removal? [y/N]: ' reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        printf 'Cancelled.\n'
        exit 0
    fi
    printf '\n'
fi

remove_gpu_passthrough() {
    local host="$1"
    local dry_run_flag=()
    local remote_command

    if [[ "$DRY_RUN" == "true" ]]; then
        dry_run_flag+=(--dry-run)
    fi

    print_action "Removing on $host..."
    print_sub "Staging removal script..."
    ssh "$host" 'rm -rf "/tmp/homelab-pve-gpu-passthrough" && mkdir -p "/tmp/homelab-pve-gpu-passthrough/lib"'
    scp -q "$SCRIPT_DIR/scripts/remove-local.sh" "$host:/tmp/homelab-pve-gpu-passthrough/remove-local.sh"
    scp -q "$REPO_ROOT/lib/print.sh" "$REPO_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-gpu-passthrough/lib/"

    print_sub "Running removal..."
    remote_command='chmod +x /tmp/homelab-pve-gpu-passthrough/remove-local.sh && if [ "$(id -u)" -ne 0 ]; then echo "Error: PVE/PBS deploy requires root SSH user" >&2; exit 1; fi && /tmp/homelab-pve-gpu-passthrough/remove-local.sh'
    if [[ ${#dry_run_flag[@]} -gt 0 ]]; then
        remote_command+=" ${dry_run_flag[*]}"
    fi
    ssh "$host" "$remote_command"

    ssh "$host" 'rm -rf "/tmp/homelab-pve-gpu-passthrough"'
}

failed_hosts=()
for host in "${HOSTS[@]}"; do
    if ! remove_gpu_passthrough "$host"; then
        failed_hosts+=("$host")
    fi
    printf '\n'
done

if [[ ${#failed_hosts[@]} -gt 0 ]]; then
    print_warn "Failed hosts: ${failed_hosts[*]}"
    exit 1
fi

if [[ "$DRY_RUN" == "false" ]]; then
    printf '\nIMPORTANT: Reboot required to apply changes:\n'
    for host in "${HOSTS[@]}"; do
        printf '  ssh %s reboot\n' "$host"
    done
fi
