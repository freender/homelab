#!/bin/bash
# Deploy SSH config to homelab infrastructure
# Usage: ./deploy.sh [host1 host2 ...] or ./deploy.sh all

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_CONFIG="${SCRIPT_DIR}/ssh_config"

# Supported hosts for this module
SUPPORTED_HOSTS=("helm" "tower" "mbp" "cottonwood" "cinci" "ace" "bray" "clovis" "xur")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping ssh (not applicable to $1)"
        exit 0
    fi
fi

# Default hosts (all accessible)
DEFAULT_HOSTS="helm tower mbp cottonwood cinci ace bray clovis xur"

HOSTS="${@:-$DEFAULT_HOSTS}"
if [[ "$1" == "all" ]]; then
    HOSTS="$DEFAULT_HOSTS"
fi

if [[ ! -f "$SSH_CONFIG" ]]; then
    echo "Error: ssh_config not found at $SSH_CONFIG"
    exit 1
fi

deploy_to_host() {
    local host="$1"
    echo "==> Deploying to $host..."
    
    # Ensure .ssh directory exists
    if ! ssh "$host" "mkdir -p ~/.ssh && chmod 700 ~/.ssh" 2>/dev/null; then
        echo "    ✗ Failed to create .ssh directory on $host"
        return 1
    fi
    
    # Copy base config
    echo "    Deploying base config..."
    scp -q "$SSH_CONFIG" "${host}:/tmp/ssh_config"
    ssh "$host" "mv /tmp/ssh_config ~/.ssh/config && chmod 600 ~/.ssh/config"
    
    # Special case: tower - append Unraid backup entry
    if [[ "$host" == "tower" ]]; then
        echo "    Appending Unraid backup config..."
        ssh "$host" "cat >> ~/.ssh/config << 'TOWER_EOF'
Host backup.unraid.net
IdentityFile ~/.ssh/unraidbackup_id_ed25519
IdentitiesOnly yes
TOWER_EOF
"
    fi
    
    # Verify deployment
    if ssh "$host" "test -f ~/.ssh/config && test -r ~/.ssh/config" 2>/dev/null; then
        echo "    ✓ Deployment successful"
    else
        echo "    ✗ Deployment verification failed"
        return 1
    fi
    
    echo ""
}

echo "==> Deploying SSH Config"
echo "    Source: $SSH_CONFIG"
echo "    Hosts: $HOSTS"
echo ""

FAILED_HOSTS=()

for host in $HOSTS; do
    if ! deploy_to_host "$host"; then
        FAILED_HOSTS+=("$host")
    fi
done

echo "==> Deployment complete!"

if [[ ${#FAILED_HOSTS[@]} -gt 0 ]]; then
    echo ""
    echo "Failed hosts: ${FAILED_HOSTS[*]}"
    exit 1
fi
