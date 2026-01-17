#!/bin/bash
# Deploy ZFS ZED Telegram notifications and ZFS scripts
# Usage: ./deploy.sh [cottonwood|cinci|tower|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Supported hosts for this module
SUPPORTED_HOSTS=("tower" "cottonwood" "cinci")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping zfs (not applicable to $1)"
        exit 0
    fi
fi
HOSTS="${@:-cottonwood cinci tower}"
if [[ "$1" == "all" ]]; then
    HOSTS="cottonwood cinci tower"
fi

# Hosts that use ZED Telegram notifications (TrueNAS/Linux only, not Unraid)
ZED_HOSTS=("cottonwood" "cinci")

# Tower-specific configuration
TOWER_DEST="/mnt/cache/appdata/scripts"
TOWER_SCRIPTS=(
    "zfs_replication_cache.sh"
    "zfs_timemachine_cleanup.sh"
    "rsync_flash.sh"
    "rsync_pi_kvm_backup.sh"
)

# Check for .env file for ZED hosts
if [[ " ${HOSTS} " =~ (cottonwood|cinci) ]]; then
    ENV_FILE="${SCRIPT_DIR}/.env"
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "Missing ${ENV_FILE}. Copy .env.example and fill in credentials."
        exit 1
    fi

    # shellcheck source=/dev/null
    source "$ENV_FILE"

    if [[ -z "${TELEGRAM_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set in ${ENV_FILE}."
        exit 1
    fi
fi

ZED_EVENTS=(
    "statechange"
    "scrub_finish"
    "resilver_finish"
    "data"
    "io_failure"
    "checksum"
)

ZFS_DEST_DIR="/mnt/cache/appdata/scripts"
ZFS_SCRIPTS=(
    "zfs_snapshots.sh"
    "zfs_replication_appdata.sh"
)

LOG_DIR="/mnt/cache/appdata/scripts/logs"
CRON_SNAPSHOT_LINE='0 0 * * * sudo /mnt/cache/appdata/scripts/zfs_snapshots.sh >> /mnt/cache/appdata/scripts/logs/zfs_snapshots.log 2>&1'
CRON_REPL_LINE='10 0 * * * sudo /mnt/cache/appdata/scripts/zfs_replication_appdata.sh >> /mnt/cache/appdata/scripts/logs/zfs_replication_appdata.log 2>&1'

update_cron() {
    local host="$1"
    ssh "$host" "(crontab -l 2>/dev/null | grep -v 'zfs_snapshots.sh' | grep -v 'zfs_replication_appdata.sh'; echo \"${CRON_SNAPSHOT_LINE}\"; echo \"${CRON_REPL_LINE}\") | crontab -"
}

deploy_to_tower() {
    echo "==> Deploying to tower (Unraid)..."
    
    echo "    Copying tower-specific scripts..."
    for script in "${TOWER_SCRIPTS[@]}"; do
        scp "${SCRIPT_DIR}/scripts/${script}" "tower:/tmp/${script}"
        ssh "tower" "sudo mv /tmp/${script} ${TOWER_DEST}/${script} && sudo chmod +x ${TOWER_DEST}/${script}"
    done
    
    echo "    Skipping ZED setup (Unraid has built-in notifications)"
    echo "    Skipping cron (User Scripts plugin handles scheduling)"
    echo "    ✓ Deployment complete for tower"
    echo ""
}

deploy_to_nas() {
    local host="$1"
    
    echo "==> Deploying to $host..."

    echo "    Copying zed-telegram-notify.sh..."
    scp "${SCRIPT_DIR}/zed-telegram-notify.sh" "${host}:/tmp/telegram-notify.sh"
    ssh "$host" "sudo mv /tmp/telegram-notify.sh /etc/zfs/zed.d/telegram-notify.sh && sudo chmod +x /etc/zfs/zed.d/telegram-notify.sh"

    echo "    Creating .env file..."
    ssh "$host" "echo -e 'TELEGRAM_TOKEN=${TELEGRAM_TOKEN}\nTELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}' | sudo tee /etc/zfs/zed.d/.env > /dev/null && sudo chmod 600 /etc/zfs/zed.d/.env"

    echo "    Creating event symlinks..."
    for event in "${ZED_EVENTS[@]}"; do
        ssh "$host" "sudo ln -sf telegram-notify.sh /etc/zfs/zed.d/${event}-telegram.sh"
        echo "      - ${event}-telegram.sh"
    done

    echo "    Restarting zfs-zed service..."
    ssh "$host" "sudo systemctl restart zfs-zed"

    if ssh "$host" "sudo systemctl is-active --quiet zfs-zed"; then
        echo "    OK zfs-zed running on $host"
    else
        echo "    WARN zfs-zed not running on $host"
        ssh "$host" "sudo systemctl status zfs-zed --no-pager -l" || true
    fi

    echo "    Ensuring sanoid/syncoid..."
    ssh "$host" "if ! command -v sanoid >/dev/null 2>&1 || ! command -v syncoid >/dev/null 2>&1; then sudo apt-get update -qq; sudo apt-get install -y sanoid; fi"

    echo "    Copying ZFS scripts..."
    ssh "$host" "mkdir -p $ZFS_DEST_DIR $LOG_DIR"
    for script in "${ZFS_SCRIPTS[@]}"; do
        scp "${SCRIPT_DIR}/scripts/${script}" "${host}:/tmp/${script}"
        ssh "$host" "sudo mv /tmp/${script} $ZFS_DEST_DIR/${script} && sudo chmod +x $ZFS_DEST_DIR/${script}"
    done

    echo "    Ensuring log directory: $LOG_DIR"

    echo "    Updating crontab..."
    update_cron "$host"

    echo "    ✓ Deployment complete for $host"
    echo ""
}

echo "==> Deploying ZFS Scripts"
echo "    Repository: https://github.com/freender/homelab"
echo "    Hosts: $HOSTS"
echo ""

for host in $HOSTS; do
    if [[ "$host" == "tower" ]]; then
        deploy_to_tower
    elif [[ " ${ZED_HOSTS[@]} " =~ " ${host} " ]]; then
        deploy_to_nas "$host"
    else
        echo "==> Skipping zfs (not applicable to $host)"
    fi
done

echo "==> Deployment complete!"
echo ""
echo "Test ZED (cottonwood/cinci) with:"
echo '  ssh <host> "sudo ZEVENT_POOL=cache ZEVENT_SUBCLASS=statechange ZEVENT_VDEV_STATE_STR=DEGRADED /etc/zfs/zed.d/telegram-notify.sh"'
