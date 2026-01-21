# docker

Docker management scripts for homelab infrastructure.

## Deployment

**Source:** https://github.com/freender/homelab

Deploy scripts from helm to hosts:

```bash
cd ~/homelab/docker
./deploy.sh all          # Deploy to all hosts
./deploy.sh tower        # Tower (Unraid) only
./deploy.sh helm         # helm only
```

## Host Registry

Per-host settings live in `docker/hosts.conf` using `docker.*` keys.

### What Gets Deployed

**All hosts** (`/mnt/cache/appdata/`):
- `start.sh` - Updates and starts Docker stacks (Traefik first), cleans up unused images
- `rm.sh` - Stops all Docker stacks with confirmation

**helm only** (`/mnt/cache/appdata/scripts/`):
- `backup.sh` - Backup appdata with smart container orchestration

### Directory Structure

```
All hosts:
  /mnt/cache/appdata/
    - start.sh, rm.sh     # Docker management scripts

helm:
  /mnt/cache/appdata/scripts/
    - backup.sh           # Backup automation
  /mnt/cache/appdata/scripts/logs/
    - backup.log          # Backup output

tower:
  /mnt/cache/appdata/scripts/  # Managed by User Scripts plugin
```

### Cron Schedules

**helm:**
- 9:05 AM daily: Backup appdata (also updates containers via start.sh)

**tower:**
- Scheduling handled by User Scripts plugin

### Manual Usage

```bash
# Quick redeployment
cd /mnt/cache/appdata && ./start.sh

# Stop all containers
cd /mnt/cache/appdata && ./rm.sh

# Manual backup (helm only)
/mnt/cache/appdata/scripts/backup.sh
```

## Scripts

### start.sh
Orchestrates Docker Compose stacks with custom startup order:
- Starts priority stacks first (Traefik)
- Pulls latest images
- Starts all remaining stacks
- Cleans up unused Docker images
- Skips directories without compose files

### rm.sh
Stops all Docker Compose stacks:
- Interactive confirmation required
- Removes orphaned containers
- Processes all subdirectories

### backup.sh
Smart backup with container orchestration (helm only):
- Stops non-critical containers
- Never stops: traefik2, socket-proxy2, crowdsec, traefik-redis2, traefik-kop2, traefik-logrotate
- Rsyncs appdata to backup location
- Restarts containers and updates images
- Verifies container health
