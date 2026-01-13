#!/bin/bash

# Enhanced backup script with never-stop container protection
# Add this script to cron:
# crontab -e
# 5 9 * * * /mnt/cache/backup/backup.sh > /mnt/cache/backup/backup.txt 2>&1
# PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin

# Define source and destination directories
SRC="/mnt/cache/appdata/"
DEST="/mnt/cache/backup/appdata/"

# Define containers that should NEVER be stopped during backup
# These are critical infrastructure containers that need to stay running
# Note: traefik2, socket-proxy2, crowdsec, traefik-redis2, traefik-kop2, and traefik-logrotate
# are all managed by the traefik2 compose file, so listing any of them protects all
NEVER_STOP_CONTAINERS=(
  "traefik2"
  "traefik-redis2"
  "traefik-kop2"
  "traefik-logrotate"
  "socket-proxy2"
  "crowdsec"
)

echo "===== Starting Docker Backup: $(date) ====="

# Create new backup directory
cd "$SRC"
mkdir -p "$DEST"

# Log never-stop containers status
echo ""
echo "Never-stop containers (will remain running):"
for container in "${NEVER_STOP_CONTAINERS[@]}"; do
  if docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
    echo "  ✓ $container"
  else
    echo "  ✗ $container (WARNING: not running!)"
  fi
done

# Get all running containers
ALL_CONTAINERS=$(docker ps -q)

# Filter out never-stop containers
CONTAINERS_TO_STOP=""
for container_id in $ALL_CONTAINERS; do
  container_name=$(docker inspect --format='{{.Name}}' $container_id | sed 's/^\///')
  
  # Check if container is in never-stop list
  if [[ ! " ${NEVER_STOP_CONTAINERS[@]} " =~ " ${container_name} " ]]; then
    CONTAINERS_TO_STOP="$CONTAINERS_TO_STOP $container_id"
  fi
done

# Log which containers will be stopped
echo ""
echo "Containers to be stopped for backup:"
if [ -n "$CONTAINERS_TO_STOP" ]; then
  for container_id in $CONTAINERS_TO_STOP; do
    container_name=$(docker inspect --format='{{.Name}}' $container_id | sed 's/^\///')
    echo "  - $container_name"
  done
else
  echo "  (none - all containers are in never-stop list)"
fi

# Stop filtered containers
if [ -n "$CONTAINERS_TO_STOP" ]; then
  echo ""
  echo "Stopping containers..."
  docker stop $CONTAINERS_TO_STOP
else
  echo ""
  echo "No containers to stop."
fi

# Backup docker appdata
echo ""
echo "Starting rsync backup..."
sudo rsync -avh --chown=1000:1000 --progress --delete "$SRC" "$DEST"

# Start stopped containers
if [ -n "$CONTAINERS_TO_STOP" ]; then
  echo ""
  echo "Restarting stopped containers..."
  docker start $CONTAINERS_TO_STOP
fi

# Update docker containers
echo ""
echo "Running start.sh to update containers..."
/mnt/cache/appdata/start.sh

# Sleep 10 seconds to allow docker containers to start
echo ""
echo "Sleeping for 10 seconds to allow docker containers to start..."
sleep 10

# Verify containers restarted
if [ -n "$CONTAINERS_TO_STOP" ]; then
  echo ""
  echo "Verifying containers restarted:"
  for container_id in $CONTAINERS_TO_STOP; do
    container_name=$(docker inspect --format='{{.Name}}' $container_id 2>/dev/null | sed 's/^\///')
    status=$(docker inspect --format='{{.State.Running}}' $container_id 2>/dev/null)
    if [ "$status" = "true" ]; then
      echo "  ✓ $container_name"
    else
      echo "  ✗ $container_name FAILED - check manually!"
    fi
  done
fi

# Clean-up
echo ""
echo "Running docker system prune..."
docker system prune -f

echo ""
echo "===== Backup Completed: $(date) ====="
