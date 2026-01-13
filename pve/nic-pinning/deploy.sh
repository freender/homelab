#!/bin/bash
# Deploy NIC pinning configs to PVE nodes
# Usage: ./deploy.sh [ace|bray|clovis|xur|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS="${@:-ace bray clovis xur}"
if [[ "$1" == "all" ]]; then
    HOSTS="ace bray clovis xur"
fi

echo "==> Deploying NIC Pinning"
echo "    Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    echo "==> Deploying to $host..."

    if [[ ! -d "$SCRIPT_DIR/$host" ]]; then
        echo "    ✗ Error: No config found for node: $host"
        echo "    Available: $(ls -d "$SCRIPT_DIR"/*/ 2>/dev/null | xargs -n1 basename | tr n  )"
        continue
    fi

    link_files=("$SCRIPT_DIR/$host"/*.link)
    if [[ ! -e "${link_files[0]}" ]]; then
        echo "    ✗ Error: No .link files found for node: $host"
        continue
    fi

    keep_files=()
    for file in "${link_files[@]}"; do
        keep_files+=("$(basename "$file")")
    done
    keep_list="$(printf "%s " "${keep_files[@]}")"

    # Ensure target directory exists
    ssh "$host" "mkdir -p /usr/local/lib/systemd/network"

    # Cleanup stale link files
    echo "    Cleaning existing .link files..."
    ssh "$host" "KEEP_FILES=\"$keep_list\"; cd /usr/local/lib/systemd/network && for file in *.link; do [ -e \"\$file\" ] || continue; keep=0; for wanted in \$KEEP_FILES; do if [ \"\$file\" = \"\$wanted\" ]; then keep=1; break; fi; done; if [ \"\$keep\" -eq 0 ]; then rm -f \"\$file\"; fi; done"

    # Copy link files
    echo "    Copying .link files..."
    scp "$SCRIPT_DIR/$host"/*.link "$host":/usr/local/lib/systemd/network/

    # Update initramfs
    echo "    Updating initramfs..."
    ssh "$host" "update-initramfs -u -k all"

    echo "    ✓ Deployed to $host (reboot required)"
    echo ""
done

echo "==> Deployment complete!"
echo ""
echo "Reboot nodes to apply changes:"
echo "  ssh <node> reboot"
