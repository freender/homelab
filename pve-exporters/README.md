# homelab/pve-exporters

Prometheus-native host metrics exporters for Proxmox and ZFS storage hosts.

## Hosts
- ace (Proxmox)
- bray (Proxmox)
- clovis (Proxmox)
- osiris (Proxmox)
- cinci-ubuntu (legacy Ubuntu/ZFS until conversion)
- cottonwood-ubuntu (legacy Ubuntu/ZFS until conversion)
- cinci / cottonwood (future PVE entries, exporter feature disabled until conversion)

## What It Collects
- Host metrics via node_exporter (CPU, memory, load, uptime, disk, network, hwmon, ZFS)
- ZFS pool capacity metrics via node_exporter textfile collector (`homelab_zpool_*`)
- SMART metrics via smartctl_exporter
- UPS metrics via apcupsd exporter on `master` / `master-standalone` UPS hosts
- Intel GPU metrics via `igpu-exporter` on selected PVE hosts

## Ports
- node_exporter: `:9100`
- smartctl_exporter: `:9633`
- apcupsd exporter: `:9162`
- igpu-exporter: `:9400` on `ace`, `bray`, and `clovis`

## Configuration Files

**On target hosts:**
- `/etc/default/prometheus-node-exporter`
- `/usr/local/bin/zfs-pool-textfile-exporter`
- `/etc/systemd/system/zfs-pool-textfile-exporter.service`
- `/etc/systemd/system/zfs-pool-textfile-exporter.timer`
- `/etc/systemd/system/smartctl-exporter.service`
- `/etc/default/smartctl-exporter`
- `/usr/local/bin/apcupsd-exporter`
- `/etc/systemd/system/apcupsd-exporter.service`
- `/etc/default/apcupsd-exporter`
- `/usr/local/bin/igpu-exporter`
- `/etc/systemd/system/igpu-exporter.service`
- `/etc/default/igpu-exporter`

**In this repo:**
- `configs/common/node-exporter.defaults`
- `configs/common/zfs-pool-textfile-exporter`
- `configs/common/zfs-pool-textfile-exporter.service`
- `configs/common/zfs-pool-textfile-exporter.timer`
- `configs/common/smartctl-exporter.defaults`
- `configs/common/smartctl-exporter.service`
- `configs/common/apcupsd-exporter.py`
- `configs/common/apcupsd-exporter.service`
- `configs/common/apcupsd-exporter.env`
- `../deploy`

`prometheus-node-exporter`, `smartmontools`, `python3`, `intel-gpu-tools`, and `golang-go` are installed via `apt` as needed. `smartctl_exporter` is fetched from the upstream GitHub release. `igpu-exporter` is built from the pinned upstream source revision because no release artifacts are published.

## Deployment

Deploy to all supported hosts:
```bash
cd ~/homelab
./deploy pve-exporters all
```

Deploy to specific hosts:
```bash
./deploy pve-exporters ace
./deploy pve-exporters bray
```

## Verification

```bash
ssh bray "systemctl status prometheus-node-exporter"
ssh bray "systemctl status smartctl-exporter"
ssh bray "curl -s http://127.0.0.1:9100/metrics | head"
ssh bray "curl -s http://127.0.0.1:9633/metrics | head"
```

## Removal

```bash
./remove.sh all
./remove.sh --purge all
```

**What it does:**
- Stops and disables smartctl-exporter service
- Stops and disables apcupsd-exporter service
- Removes smartctl-exporter binary and config
- Removes apcupsd-exporter binary and config
- Optionally purges node_exporter package
