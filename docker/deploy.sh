#!/bin/bash
# Deploy docker management scripts
# Usage: ./deploy.sh [helm|cottonwood|cinci|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS="${@:-helm cottonwood cinci}"
if [[ "$1" == "all" ]]; then
    HOSTS="helm cottonwood cinci"
fi

# Host-specific configuration
declare -A HOST_USERS=(
    ["helm"]="freender"
    ["cottonwood"]="denys"
    ["cinci"]="sysadm"
)

# Hosts without ZFS (need backup.sh)
NO_ZFS_HOSTS=("helm")

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
    
    # Update cron entries
    echo "    Updating crontab..."
    if [[ "$host" == "helm" ]]; then
        # helm: backup.sh cron only (it calls start.sh internally)
        ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh' | grep -v 'backup.sh' | grep -v '/mnt/ssdpool/backup' | grep -v 'snapshot_ceph' | grep -v 'traefik-acme-sync'; echo \"${CRON_BACKUP_HELM}\") | crontab -"
    else
        # NAS hosts: start.sh cron (keep existing update.log location)
        ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh'; echo \"${CRON_START_NAS}\") | crontab -"
    fi
    
    echo "    ✓ Deployment complete for $host"
    echo ""
}

echo "==> Deploying Docker Management Scripts"
echo "    Repository: https://github.com/freender/homelab-vm"
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
