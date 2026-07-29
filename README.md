# Homelab Infrastructure

Automation and configuration management for Proxmox-based homelab infrastructure.

## Overview

**Hardware:** Proxmox cluster, standalone Proxmox nodes, LXCs, remote Ubuntu NAS

**Network:**
- Home: `*.freender.internal`
- Remote Ubuntu offsite hosts: `cottonwood`, `cinci` (baremetal Ubuntu, Docker/PBS DR targets)
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
- UPS state exported by apcupsd-exporter; alerting via the vmalert `ups` group

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
- PBS storage tokens are rendered from 1Password at deploy time and staged to the
  target host; no secret file needs to be placed on the host by hand.

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
- Timezone
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

### [zfs-automation](zfs-automation/)
ZFS snapshots, scrub, health checks, and replication
- Sanoid-style snapshot plans, scrub and health-check timers
- Replication jobs (pull and push), including dynamic LXC sources
- SSH forced-command allow-lists (`homelab-zfs-send-only` / `homelab-zfs-receive-only`)
  restrict what a peer may `zfs send`/`receive`
- Supports module-wide `paused: true` and per-job `replication_jobs.<job>.paused: true`

```bash
./deploy zfs-automation all
```

### [pbs-client-backup](pbs-client-backup/)
Host-level PBS client backups (appdata, `/etc/pve`, system files)
- Secrets rendered from 1Password into tmpfs at deploy time
- Supports `paused: true`

```bash
./deploy pbs-client-backup all
```

### [pve-http-boot](pve-http-boot/)
iPXE/HTTP boot server for unattended Proxmox installs
- Serves baked ISO artifacts and iPXE scripts

```bash
./deploy pve-http-boot all
```

### [pve-autoinstall](pve-autoinstall/)
Unattended Proxmox installer answer files
- Matches a machine by `dmi_uuid` and installs to `boot_disk_serial`
- Driven via PDM; answer files and root passwords are staged from 1Password

```bash
./deploy pve-autoinstall all
```

### [ubuntu-setup](ubuntu-setup/)
Ubuntu OS setup for the offsite hosts (cinci, cottonwood)
- Docker CE, sudoers, SSH hardening, ZFS ARC limit, inotify limits
- Optional WireGuard and Samba

```bash
./deploy ubuntu-setup all
```

### [keepalived](keepalived/)
VRRP virtual IP for the Traefik HA frontend

```bash
./deploy keepalived all
```

### [disk-spindown](disk-spindown/)
HDD idle spindown and wakeup timers
- Supports `paused: true`

```bash
./deploy disk-spindown all
```

### [pve-notifications](pve-notifications/)
PVE notification endpoints and matchers (Telegram webhook)

```bash
./deploy pve-notifications all
```

### [pve-postinstall-webhook](pve-postinstall-webhook/)
Post-install webhook that triggers a deploy when a node finishes autoinstall

```bash
./deploy pve-postinstall-webhook all
```

### [pve-realtek-r8152-dkms](pve-realtek-r8152-dkms/)
DKMS build of the Realtek r8152 USB NIC driver
- The generic drivers are blacklisted only when the DKMS build succeeds, so a failed
  build can never leave the host without a working NIC driver

```bash
./deploy pve-realtek-r8152-dkms all
```

### Proxmox Upstream Patches
Local patches for Proxmox behavior that should be removed when equivalent
upstream fixes ship:

- [pve-zfs-large-block-patch](pve-zfs-large-block-patch/): Bug 4603 - Add support for migrating ZFS datasets with large_blocks
- [pve-zfs-migration-sync-patch](pve-zfs-migration-sync-patch/): Bug 7653 - target-side ZFS receive cache mitigation for LXC migration
- [pve-lxc-pre-replication-patch](pve-lxc-pre-replication-patch/): pre-replication hook for LXC guests

Revert these local patches and remove the modules after the corresponding
Proxmox fixes are included upstream.

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
