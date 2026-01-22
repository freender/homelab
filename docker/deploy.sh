#!/bin/bash
# Deploy docker management scripts
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"

# --- Host Selection ---
SUPPORTED_HOSTS=($(hosts list --feature docker))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping docker (not applicable to $1)"
    exit 0
fi

# --- Configuration ---
APPDATA_DEST="/mnt/cache/appdata"
APPDATA_SCRIPTS_DIR="${APPDATA_DEST}/scripts"
APPDATA_LOGS_DIR="${APPDATA_SCRIPTS_DIR}/logs"

CRON_START_NAS='0 9 * * * cd /mnt/cache/appdata && ./start.sh >> /mnt/cache/appdata/update.log 2>&1'
CRON_BACKUP_HELM='5 9 * * * /mnt/cache/appdata/scripts/backup.sh >> /mnt/cache/appdata/scripts/logs/backup.log 2>&1'

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local user
    
    user=$(hosts get "$host" "docker.user") || { print_warn "docker.user missing"; return 1; }
    
    # Deploy appdata scripts
    print_sub "Deploying scripts (user: $user)..."
    for script in start.sh rm.sh; do
        scp -q "${SCRIPT_DIR}/${script}" "${host}:/tmp/${script}"
        ssh "$host" "sudo mv /tmp/${script} ${APPDATA_DEST}/${script} && sudo chown ${user}:${user} ${APPDATA_DEST}/${script} && sudo chmod +x ${APPDATA_DEST}/${script}"
    done
    
    # Deploy backup.sh if needed
    if hosts has "$host" "docker-backup"; then
        print_sub "Deploying backup.sh..."
        ssh "$host" "mkdir -p ${APPDATA_SCRIPTS_DIR} ${APPDATA_LOGS_DIR}"
        scp -q "${SCRIPT_DIR}/backup.sh" "${host}:/tmp/backup.sh"
        ssh "$host" "mv /tmp/backup.sh ${APPDATA_SCRIPTS_DIR}/backup.sh && chmod +x ${APPDATA_SCRIPTS_DIR}/backup.sh"
    fi
    
    # Update cron (skip for User Scripts plugin hosts)
    if ! hosts has "$host" "docker-no-cron"; then
        print_sub "Updating crontab..."
        if hosts has "$host" "docker-backup"; then
            ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh' | grep -v 'backup.sh' | grep -v '/mnt/ssdpool/backup' | grep -v 'snapshot_ceph' | grep -v 'traefik-acme-sync'; echo \"${CRON_BACKUP_HELM}\") | crontab -"
        else
            ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh'; printf '%s\n' \"${CRON_START_NAS}\") | crontab -"
        fi
    else
        print_sub "Skipping cron (User Scripts plugin)"
    fi
}

# --- Main ---
deploy_init "Docker Management Scripts"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Manual usage:"
echo "  cd /mnt/cache/appdata && ./start.sh   # Update and start containers"
echo "  cd /mnt/cache/appdata && ./rm.sh      # Stop all containers"
