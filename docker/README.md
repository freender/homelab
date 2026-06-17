# docker

Docker management scripts for homelab infrastructure.

## Deployment

**Source:** https://github.com/freender/homelab

Deploy scripts from helm to hosts:

```bash
cd ~/homelab
./deploy docker all
./deploy docker tower
./deploy docker helm
./deploy docker neo
```

## Host Registry

Per-host settings live in `hosts.conf` using `docker.*` keys.

### What Gets Deployed

**All hosts** (`/mnt/cache/appdata/`):
- `start.sh` - Updates and starts Docker stacks (Traefik first), then safely prunes unused images after update runs
- `rm.sh` - Stops all Docker stacks with confirmation

**Hosts with update schedules configured** (`/etc/systemd/system/`):
- `homelab-docker-update.service`
- `homelab-docker-update.timer`

### Directory Structure

```
All hosts:
  /mnt/cache/appdata/
    - start.sh, rm.sh     # Docker management scripts

Hosts with update schedules configured:
  /etc/systemd/system/
    - homelab-docker-update.service
    - homelab-docker-update.timer

tower:
  /mnt/cache/appdata/scripts/  # Managed by User Scripts plugin
```

### Update Schedule

Docker auto-update is handled by `homelab-docker-update.timer`, rendered from `hosts.conf` via `docker.update_schedule`. The timer runs `start.sh`, which pulls images before `docker compose up -d` by default.

After a successful pull/update run, `start.sh` prunes unused Docker images only. It skips pruning when any stack failed or when any stopped container exists, and it never prunes Docker networks or volumes. Use `--no-prune` to disable image pruning for a run.

Docker boot/start orchestration is intentionally not managed here. Containers should use native Docker restart policies for boot, reboot, and HA migration recovery.

## Traefik Sync

- `tower` runs `traefik-sync-tower` in source mode on `net_overlay`
- `helm` runs `traefik-sync-helm` in client mode
- `neo` runs `traefik-sync-neo` in client mode
- Clients pull `acme.json` and `fileConfig.yml` over overlay HTTP from `http://traefik-sync-tower:8080`
- Auth uses `TRAEFIK_SYNC_API_TOKEN` in the Traefik stack `.env`
- `start.sh` is deployed to all docker hosts, including `neo`, so Traefik stack updates can be applied with `cd /mnt/cache/appdata && ./start.sh`

### Manual Usage

```bash
# Quick redeployment
cd /mnt/cache/appdata && ./start.sh

# Stop all containers
cd /mnt/cache/appdata && ./rm.sh
```

## Scripts

### start.sh
Orchestrates Docker Compose stacks with custom startup order:
- Starts priority stacks first (Traefik)
- Pulls latest images
- Starts all remaining stacks
- Cleans up unused Docker images after safe update runs
- Skips directories without compose files

### rm.sh
Stops all Docker Compose stacks:
- Interactive confirmation required
- Removes orphaned containers
- Processes all subdirectories
