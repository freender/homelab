#!/bin/bash
# Deploy docker management scripts
# Usage: ./deploy.sh [helm|cottonwood|cinci|tower|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Supported hosts for this module
SUPPORTED_HOSTS=("helm" "tower" "cottonwood" "cinci")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping docker (not applicable to $1)"
        exit 0
    fi
fi
HOSTS="${@:-helm cottonwood cinci tower}"
if [[ "$1" == "all" ]]; then
    HOSTS="helm cottonwood cinci tower"
fi

# Host-specific configuration
declare -A HOST_USERS=(
    ["helm"]="freender"
    ["cottonwood"]="denys"
    ["cinci"]="sysadm"
    ["tower"]="root"
)

# Hosts without ZFS (need backup.sh)
NO_ZFS_HOSTS=("helm")

# Hosts that don't need cron (User Scripts plugin handles scheduling)
NO_CRON_HOSTS=("tower")

APPDATA_DEST="/mnt/cache/appdata"
DOCKER_SCRIPTS_DIR="\$HOME/docker-scripts"
DOCKER_LOGS_DIR="\$HOME/docker-logs"

APPDATA_SCRIPTS=(
    "start.sh"
    "rm.sh"
)

BACKUP_SCRIPTS=(
    "backup.sh"
)

# Cron entries
CRON_START_NAS='0 9 * * * cd /mnt/cache/appdata && ./start.sh >> /mnt/cache/appdata/update.log 2>&1'
CRON_BACKUP_HELM='5 9 * * * $HOME/docker-scripts/backup.sh >> $HOME/docker-logs/backup.log 2>&1'

deploy_to_host() {
    local host="$1"
    local user="${HOST_USERS[$host]}"
    
    echo "==> Deploying to $host (user: $user)..."
    
    # Deploy appdata scripts (all hosts)
    echo "    Copying appdata scripts..."
    for script in "${APPDATA_SCRIPTS[@]}"; do
        scp "${SCRIPT_DIR}/${script}" "${host}:/tmp/${script}"
        ssh "$host" "sudo mv /tmp/${script} ${APPDATA_DEST}/${script} && sudo chown ${user}:${user} ${APPDATA_DEST}/${script} && sudo chmod +x ${APPDATA_DEST}/${script}"
    done
    
    # Deploy backup.sh only to non-ZFS hosts (helm)
    if [[ " ${NO_ZFS_HOSTS[@]} " =~ " ${host} " ]]; then
        echo "    Creating docker-scripts directory..."
        ssh "$host" "mkdir -p ${DOCKER_SCRIPTS_DIR}"
        
        echo "    Creating docker-logs directory..."
        ssh "$host" "mkdir -p ${DOCKER_LOGS_DIR}"
        
        echo "    Copying backup.sh..."
        for script in "${BACKUP_SCRIPTS[@]}"; do
            scp "${SCRIPT_DIR}/${script}" "${host}:/tmp/${script}"
            ssh "$host" "mv /tmp/${script} ${DOCKER_SCRIPTS_DIR}/${script} && chmod +x ${DOCKER_SCRIPTS_DIR}/${script}"
        done
    fi
    
    # Update cron entries (skip for hosts using User Scripts plugin)
    if [[ ! " ${NO_CRON_HOSTS[@]} " =~ " ${host} " ]]; then
        echo "    Updating crontab..."
        if [[ "$host" == "helm" ]]; then
            # helm: backup.sh cron only (it calls start.sh internally)
            ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh' | grep -v 'backup.sh' | grep -v '/mnt/ssdpool/backup' | grep -v 'snapshot_ceph' | grep -v 'traefik-acme-sync'; echo "${CRON_BACKUP_HELM}") | crontab -"
        else
            # NAS hosts: start.sh cron (keep existing update.log location)
            ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh'; echo "${CRON_START_NAS}") | crontab -"
        fi
    else
        echo "    Skipping cron (User Scripts plugin handles scheduling)"
    fi
    
    echo "    ✓ Deployment complete for $host"
    echo ""
}

echo "==> Deploying Docker Management Scripts"
echo "    Repository: https://github.com/freender/homelab"
echo "    Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    deploy_to_host "$host"
done

echo "==> All deployments complete!"
echo ""
echo "Manual usage:"
echo "  cd /mnt/cache/appdata && ./start.sh   # Update and start containers"
echo "  cd /mnt/cache/appdata && ./rm.sh      # Stop all containers"
