# Homelab Infrastructure

Automation and configuration management for Proxmox-based homelab infrastructure.

## Overview

**Hardware:** 3-node Proxmox Ceph cluster, backup server (PBS), VMs, remote NAS

**Network:**
- Home: `*.freender.internal`
- Remote: `cottonwood.internal`, `cinci.internal`
- VIP for Traefik HA

## Modules

### [apcupsd](apcupsd/)
UPS monitoring with coordinated cluster shutdown
- Master/slave configuration
- Telegram notifications

**Deploy:**
```bash
cd ~/homelab/apcupsd
./scripts/deploy.sh <host>
```

### [telegraf](telegraf/)
Metrics collection (CPU, disk, network, sensors, smartctl)
- Sends to VictoriaMetrics
- Deploys to Proxmox hosts

**Deploy:**
```bash
cd ~/homelab/telegraf
./deploy.sh all
```

### [zfs](zfs/)
ZFS automation for remote NAS
- ZED Telegram notifications
- Automated snapshots (7 daily, 4 weekly, 3 monthly)
- Appdata replication

**Deploy:**
```bash
cd ~/homelab/zfs
./deploy.sh all
```

### [docker](docker/)
Docker management scripts
- `start.sh`: Update and start all containers (Traefik first)
- `rm.sh`: Stop all containers
- `backup.sh`: Backup appdata with container orchestration

**Deploy:**
```bash
cd ~/homelab/docker
./deploy.sh all
```

### [ssh](ssh/)
SSH config auto-deployment
- Uses `*.freender.internal` DNS for home
- Uses `*.internal` DNS for remote sites

**Deploy:**
```bash
cd ~/homelab/ssh
./deploy.sh all
```

## Quick Reference

**Clone repo:**
```bash
git clone git@github.com:freender/homelab.git ~/homelab
```

**Update all modules:**
```bash
cd ~/homelab && git pull
```

**Common commands:**
```bash
# UPS status
ssh <host> "apcaccess status"

# Metrics query
curl -s 'http://victoria-metrics.freender.internal:8428/api/v1/query?query=sensors_temp_input' | jq

# ZFS snapshots
ssh <remote-nas> "zfs list -t snapshot"

# Docker containers
ssh <vm> "cd /mnt/cache/appdata && ./start.sh"
```
