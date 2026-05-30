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
./deploy all all
./deploy all ace
./deploy --dry-run all all
./deploy --force all all
```

Flags supported by `./deploy`:
- `--dry-run`, `-n`: preview actions only
- `--force`, `--force-update`: overwrite managed files even when content matches

Run `./deploy` and `./validate` from the repo root. The wrappers prefer repo `.venv`, then `uv run`, then plain `python3`, so the same commands work on both `exo` and `riven`.

## Modules

### [apcupsd](apcupsd/)
UPS monitoring with coordinated cluster shutdown
- Master/slave configuration
- Telegram notifications

```bash
./deploy apcupsd all
```

### [pve-exporters](pve-exporters/)
Prometheus-native host metrics for Proxmox nodes, including UPS metrics on apcupsd master nodes
- Exposes local UPS metrics on `:9162`
- Scraped by vmagent/VictoriaMetrics

```bash
./deploy pve-exporters all
```

### [apt-upgrade](apt-upgrade/)
Remote apt dist-upgrade across PVE and Ubuntu hosts

```bash
./deploy apt-upgrade all
```

### [docker](docker/)
Docker management scripts
- `start.sh`: Update and start all containers (Traefik first)
- `rm.sh`: Stop all containers
- Optional systemd timer for Docker updates

```bash
./deploy docker all
```

### [pve-backup](pve-backup/)
PVE backup configuration
- Standalone PBS storage definitions and backup jobs
- Subfeatures (configured in `hosts.conf` under `pve-backup`):
  - `pbs_setup`: standalone PBS storage definitions and backup jobs
  - `restore_lxc_configs`: opt-in restore of selected standalone LXC configs from the staged `/etc/pve` PBS backup; autostart defaults to disabled
- Requires:
  - `/etc/homelab/pbs-tokens.env` on target host (see `pve-backup/configs/pbs-tokens.env.example`) for standalone PBS storage auth

PVE `/etc/pve` config backups are configured through `pbs-client-backup`.

```bash
./deploy pve-backup all
```

### [pve-gpu-passthrough](pve-gpu-passthrough/)
Proxmox GPU passthrough configs
- Updates boot cmdline, VFIO modules, and modprobe configs
- Safety checks: managed cmdline must include `root=ZFS=rpool/ROOT/pve-1` and target host must have dataset `rpool/ROOT/pve-1`, or deploy/install aborts
- Requires reboot after deploy

```bash
./deploy pve-gpu-passthrough all
```

### [pve-postinstall](pve-postinstall/)
PVE post-install configuration
- No-subscription repo sources, nag removal
- Timezone, Ceph reconciliation
- Subfeatures (configured in `hosts.conf` under `pve-postinstall`):
  - `interfaces`: per-node `/etc/network/interfaces` rendering

```bash
./deploy pve-postinstall all
```

### [pve-interface-pinning](pve-interface-pinning/)
PVE physical NIC naming and Wake-on-LAN configuration
- Installs systemd `.link` files for repo-owned `nicN` names by MAC address
- Enables WOL on configured wired interfaces without modeling Wi-Fi devices

```bash
./deploy pve-interface-pinning all
```

### [ssh-config](ssh-config/)
SSH config auto-deployment
- Uses `*.freender.internal` DNS for home
- Uses `*.internal` DNS for remote sites

```bash
./deploy ssh-config all
```

## Quick Reference

```bash
# Clone
git clone git@github.com:freender/homelab.git ~/homelab

# Update
cd ~/homelab && git pull

# Validate (python compile + shellcheck + YAML + dry-run)
./validate

# Single module dry-run
./deploy --dry-run <module> <host>

# Single module forced deploy
./deploy --force <module> <host>

# UPS status
ssh <host> "apcaccess status"

# Docker containers
ssh <vm> "cd /mnt/cache/appdata && ./start.sh"
```
