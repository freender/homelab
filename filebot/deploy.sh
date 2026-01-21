#!/bin/bash
# Deploy filebot scripts
# Usage: ./deploy.sh [tower|all]

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

get_filebot_hosts() {
    get_hosts_with_feature filebot
}

SUPPORTED_HOSTS=($(get_filebot_hosts))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping filebot (not applicable to $1)"
    exit 0
fi

DEST_DIR="/mnt/cache/appdata/scripts"
SCRIPTS=(
    "filebot_monitor.sh"
)

print_action "Deploying Filebot Scripts"
print_sub "Destination: ${DEST_DIR}"
echo ""

for host in $HOSTS; do
    print_action "Deploying to ${host}..."

    print_sub "Copying scripts..."
    for script in "${SCRIPTS[@]}"; do
        scp -q "${SCRIPT_DIR}/${script}" "${host}:/tmp/${script}"
        ssh "$host" "sudo mv /tmp/${script} ${DEST_DIR}/${script} && sudo chmod +x ${DEST_DIR}/${script}"
    done

    print_ok "Deployment complete"
done

echo ""
echo "Scheduling handled by User Scripts plugin on tower."
echo "Update User Scripts wrappers to point to:"
echo "  /mnt/cache/appdata/scripts/filebot_monitor.sh"
