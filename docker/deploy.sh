#!/bin/bash
# Deploy docker management scripts
# Usage: ./deploy.sh [helm|tower|all]

# Source shared library
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! load_hosts_file "$SCRIPT_DIR/hosts.conf"; then
    exit 1
fi

get_docker_hosts() {
    get_hosts_with_feature docker
}

load_host_config() {
    local host="$1"
    USER=$(get_host_kv "$host" "docker.user")

    if [[ -z "$USER" ]]; then
        echo "ERROR: docker.user missing for $host in docker/hosts.conf"
        return 1
    fi
}

host_needs_backup() {
    local host="$1"
    host_has_feature "$host" "docker-backup"
}

host_skip_cron() {
    local host="$1"
    host_has_feature "$host" "docker-no-cron"
}

SUPPORTED_HOSTS=($(get_docker_hosts))

# Filter hosts based on arguments
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping docker (not applicable to $1)"
    exit 0
fi

# Configuration
APPDATA_DEST="/mnt/cache/appdata"
APPDATA_SCRIPTS_DIR="${APPDATA_DEST}/scripts"
APPDATA_LOGS_DIR="${APPDATA_SCRIPTS_DIR}/logs"

APPDATA_SCRIPTS=(
    "start.sh"
    "rm.sh"
)

BACKUP_SCRIPTS=(
    "backup.sh"
)

# Cron entries
CRON_START_NAS='0 9 * * * cd /mnt/cache/appdata && ./start.sh >> /mnt/cache/appdata/update.log 2>&1'
CRON_BACKUP_HELM='5 9 * * * /mnt/cache/appdata/scripts/backup.sh >> /mnt/cache/appdata/scripts/logs/backup.log 2>&1'

deploy_to_host() {
    local host="$1"

    if ! load_host_config "$host"; then
        return 1
    fi

    print_action "Deploying to $host (user: $USER)..."
    
    # Deploy appdata scripts (all hosts)
    print_sub "Copying appdata scripts..."
    for script in "${APPDATA_SCRIPTS[@]}"; do
        scp -q "${SCRIPT_DIR}/${script}" "${host}:/tmp/${script}"
        ssh "$host" "sudo mv /tmp/${script} ${APPDATA_DEST}/${script} && sudo chown ${USER}:${USER} ${APPDATA_DEST}/${script} && sudo chmod +x ${APPDATA_DEST}/${script}"
    done
    
    # Deploy backup.sh only to hosts that need it
    if host_needs_backup "$host"; then
        print_sub "Creating appdata scripts directories..."
        ssh "$host" "mkdir -p ${APPDATA_SCRIPTS_DIR} ${APPDATA_LOGS_DIR}"
        
        print_sub "Copying backup.sh..."
        for script in "${BACKUP_SCRIPTS[@]}"; do
            scp -q "${SCRIPT_DIR}/${script}" "${host}:/tmp/${script}"
            ssh "$host" "mv /tmp/${script} ${APPDATA_SCRIPTS_DIR}/${script} && chmod +x ${APPDATA_SCRIPTS_DIR}/${script}"
        done
    fi
    
    # Update cron entries (skip for hosts using User Scripts plugin)
    if ! host_skip_cron "$host"; then
        print_sub "Updating crontab..."
        if host_needs_backup "$host"; then
            # Hosts with backup: backup.sh cron only (it calls start.sh internally)
            ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh' | grep -v 'backup.sh' | grep -v '/mnt/ssdpool/backup' | grep -v 'snapshot_ceph' | grep -v 'traefik-acme-sync'; echo \"${CRON_BACKUP_HELM}\") | crontab -"
        else
            # NAS hosts: start.sh cron
            ssh "$host" "(crontab -l 2>/dev/null | grep -v 'start.sh'; printf '%s\n' \"${CRON_START_NAS}\") | crontab -"
        fi
    else
        print_sub "Skipping cron (User Scripts plugin handles scheduling)"
    fi
    
    print_ok "Deployment complete for $host"
    echo ""
}

print_action "Deploying Docker Management Scripts"
print_sub "Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    deploy_to_host "$host"
done

print_action "All deployments complete!"
echo ""
echo "Manual usage:"
echo "  cd /mnt/cache/appdata && ./start.sh   # Update and start containers"
echo "  cd /mnt/cache/appdata && ./rm.sh      # Stop all containers"
