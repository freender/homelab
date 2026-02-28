# Homelab Infrastructure

Automation and configuration management for Proxmox-based homelab infrastructure.

## Overview

**Hardware:** Proxmox Ceph cluster, standalone Proxmox node, VMs, remote NAS

**Network:**
- Home: `*.freender.internal`
- Remote (Ubuntu): `cottonwood.internal`, `cinci.internal`
- VIP for Traefik HA

## Deploy All

```bash
cd ~/homelab
./deploy-all.sh
```

## Modules

### [apcupsd](apcupsd/)
UPS monitoring with coordinated cluster shutdown
- Master/slave configuration
- Telegram notifications

**Deploy:**
```bash
cd ~/homelab/apcupsd
./deploy.sh all
```

**Remove:**
```bash
cd ~/homelab/apcupsd
./remove.sh all
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

### [pve-interfaces](pve-interfaces/)
Proxmox network interface configuration
- Per-node `/etc/network/interfaces` files

**Deploy:**
```bash
cd ~/homelab/pve-interfaces
./deploy.sh all
```

### [pve-gpu-passthrough](pve-gpu-passthrough/)
Proxmox GPU passthrough configs
- Updates boot cmdline, VFIO modules, and modprobe configs

**Deploy:**
```bash
cd ~/homelab/pve-gpu-passthrough
./deploy.sh all
```

**Remove:**
```bash
cd ~/homelab/pve-gpu-passthrough
./remove.sh all
```

### [pve-notifications](pve-notifications/)
Proxmox notification targets and matchers
- Deploys `/etc/pve/notifications.cfg` and `/etc/pve/priv/notifications.cfg`
- Uses Telegram credentials from `pve-notifications/configs/telegram.env` (not tracked)

**Deploy:**
```bash
cd ~/homelab/pve-notifications
cp configs/telegram.env.example configs/telegram.env
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
curl -s "http://victoria-metrics.freender.internal:8428/api/v1/query?query=sensors_temp_input" | jq

# Docker containers
ssh <vm> "cd /mnt/cache/appdata && ./start.sh"
```
### [telegraf](telegraf/)
Metrics collection (CPU, disk, network, sensors, smartctl)
- Sends to VictoriaMetrics
- Deploys to ace, bray, clovis, osiris

**Deploy:**
```bash
cd ~/homelab/telegraf
./deploy.sh all
```

**Remove:**
```bash
cd ~/homelab/telegraf
./remove.sh all
```
