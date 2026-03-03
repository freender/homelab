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
./deploy-all.sh          # all modules, all hosts
./deploy-all.sh ace      # all applicable modules, single host
./deploy-all.sh --dry-run all   # dry-run everything
```

## Modules

### [apcupsd](apcupsd/)
UPS monitoring with coordinated cluster shutdown
- Master/slave configuration
- Telegram notifications

```bash
cd ~/homelab/apcupsd && ./deploy.sh all
```

### [apt-upgrade](apt-upgrade/)
Remote apt dist-upgrade across PVE and Ubuntu hosts

```bash
cd ~/homelab/apt-upgrade && ./deploy.sh all
```

### [docker](docker/)
Docker management scripts
- `start.sh`: Update and start all containers (Traefik first)
- `rm.sh`: Stop all containers
- `backup.sh`: Backup appdata with container orchestration

```bash
cd ~/homelab/docker && ./deploy.sh all
```

### [pve-gpu-passthrough](pve-gpu-passthrough/)
Proxmox GPU passthrough configs
- Updates boot cmdline, VFIO modules, and modprobe configs
- Requires reboot after deploy

```bash
cd ~/homelab/pve-gpu-passthrough && ./deploy.sh all
```

### [pve-interfaces](pve-interfaces/)
Proxmox network interface configuration
- Per-node `/etc/network/interfaces` from templates

```bash
cd ~/homelab/pve-interfaces && ./deploy.sh all
```

### [pve-notifications](pve-notifications/)
Proxmox notification targets and matchers
- Deploys `/etc/pve/notifications.cfg` and `/etc/pve/priv/notifications.cfg`
- Uses Telegram credentials from `configs/telegram.env` (not tracked)

```bash
cp pve-notifications/configs/telegram.env.example pve-notifications/configs/telegram.env
cd ~/homelab/pve-notifications && ./deploy.sh all
```

### [pve-postinstall](pve-postinstall/)
PVE post-install configuration
- No-subscription repo sources, nag removal
- Timezone, local-zfs storage, Ceph reconciliation
- Backup subfeatures (configured in `hosts.conf` under `pve-postinstall.backup`):
  - `cluster`: `/etc/pve` backup to PBS with systemd timer (cluster scope)
  - `standalone`: PBS storage definitions + backup jobs (osiris standalone scope)
- Requires:
  - `pve-postinstall/configs/pbs.env` (from `pbs.env.example`) for cluster config backup
  - `/etc/homelab/pbs-tokens.env` on target host (see `pve-postinstall/configs/pbs-tokens.env.example`) for standalone PBS storage auth

```bash
cd ~/homelab/pve-postinstall && ./deploy.sh all
```

### [ssh](ssh/)
SSH config auto-deployment
- Uses `*.freender.internal` DNS for home
- Uses `*.internal` DNS for remote sites

```bash
cd ~/homelab/ssh && ./deploy.sh all
```

### [telegraf](telegraf/)
Metrics collection (CPU, disk, network, sensors, smartctl)
- Sends to VictoriaMetrics

```bash
cd ~/homelab/telegraf && ./deploy.sh all
```

## Quick Reference

```bash
# Clone
git clone git@github.com:freender/homelab.git ~/homelab

# Update
cd ~/homelab && git pull

# Validate (shellcheck + YAML + dry-run)
./validate.sh

# Single module dry-run
cd <module> && ./deploy.sh --dry-run <host>

# UPS status
ssh <host> "apcaccess status"

# Docker containers
ssh <vm> "cd /mnt/cache/appdata && ./start.sh"
```
