# homelab/pve-exporters

Prometheus-native host metrics exporters for Proxmox nodes.

## Hosts
- ace (Proxmox)
- bray (Proxmox)
- clovis (Proxmox)

## What It Collects
- Host metrics via node_exporter (CPU, memory, load, uptime, disk, network, hwmon, ZFS)
- SMART metrics via smartctl_exporter

## Ports
- node_exporter: `:9100`
- smartctl_exporter: `:9633`

## Configuration Files

**On target hosts:**
- `/etc/default/prometheus-node-exporter`
- `/etc/systemd/system/smartctl-exporter.service`
- `/etc/default/smartctl-exporter`

**In this repo:**
- `configs/common/node-exporter.env`
- `configs/common/smartctl-exporter.env`
- `configs/common/smartctl-exporter.service`
- `deploy.sh`

## Deployment

Deploy to all supported hosts:
```bash
cd ~/homelab/pve-exporters
./deploy.sh all
```

Deploy to specific hosts:
```bash
./deploy.sh ace bray
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
- Removes smartctl-exporter binary and config
- Optionally purges node_exporter package
