# homelab/telegraf

Minimal Telegraf configuration for collecting hardware metrics from selected hosts.

## Hosts
- ace (Proxmox)
- bray (Proxmox)
- clovis (Proxmox)
- xur (PBS)

## What It Collects
- CPU package temperature (coretemp)
- Individual core temperatures
- NVMe drive temperatures
- Other thermal sensors
- Memory metrics (total, available, used, free, cached, buffered, used_percent)
- SMART disk metrics (temperature, health)
- Disk I/O metrics
- Network interface metrics
- UPS metrics via apcupsd input (APC role only)
- **Collection interval:** 10 seconds
- Sends to VictoriaMetrics at `victoria-metrics.freender.internal:8428`
- Database: `telegraf`

## Configuration Files

**On target hosts:**
- `/etc/telegraf/telegraf.conf` - Main configuration (10s interval, output to VictoriaMetrics)
- `/etc/telegraf/telegraf.d/` - Input plugins (sensors, mem, smartctl, diskio, net, apcupsd)

**In this repo:**
- `configs/common/telegraf.conf` - Main Telegraf configuration template
- `configs/common/sensors.conf` - Sensors input plugin configuration
- `configs/common/mem.conf` - Memory metrics input plugin configuration
- `configs/common/smartctl.conf` - SMART monitoring input plugin configuration
- `configs/common/diskio.conf` - Disk I/O monitoring configuration
- `configs/common/net.conf` - Network interface monitoring configuration
- `configs/roles/apc/apcupsd.conf` - UPS monitoring input plugin (APC role)
- `deploy.sh` - Deployment script

## Deployment

Deploy to all hosts:
```bash
cd ~/homelab/telegraf
./deploy.sh all
```

Deploy to specific hosts:
```bash
./deploy.sh ace bray
```

## Verification

Check service on a host:
```bash
ssh bray "systemctl status telegraf"
ssh bray "journalctl -u telegraf -f"
```

Test configuration:
```bash
ssh bray "telegraf --test --config /etc/telegraf/telegraf.conf"
```

## Removal

Remove from specific hosts:
```bash
./remove.sh ace bray
```

Remove from all hosts:
```bash
./remove.sh all
```

Also remove telegraf package:
```bash
./remove.sh --purge all
```

Also remove InfluxData apt repository:
```bash
./remove.sh --remove-repo all
```

Skip confirmation prompt:
```bash
./remove.sh --yes all
```

**What it does:**
- Stops and disables the telegraf service
- Backs up `/etc/telegraf/` to `/etc/telegraf.bak.TIMESTAMP`
- Removes config files and sudoers rule
- Optionally removes the InfluxData repository with `--remove-repo`
- Optionally purges the telegraf package with `--purge`

## Roles

- `telegraf`: Generic hardware metrics
- `telegraf-apc`: Adds `apcupsd` input for UPS metrics
