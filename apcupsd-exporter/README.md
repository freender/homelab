# homelab/apcupsd-exporter

Prometheus exporter for local `apcaccess` metrics on UPS master nodes.

## Hosts
- bray (Proxmox)
- osiris (Proxmox)

## What It Collects
- UPS load percent
- Battery charge percent
- Time left
- Line voltage
- Battery voltage
- UPS status and metadata labels

## Port
- apcupsd_exporter: `:9162`

## Configuration Files

**On target hosts:**
- `/etc/default/apcupsd-exporter`
- `/etc/systemd/system/apcupsd-exporter.service`
- `/usr/local/bin/apcupsd-exporter`

## Deployment

Deploy to all supported hosts:
```bash
cd ~/homelab/apcupsd-exporter
./deploy.sh all
```

Deploy to specific hosts:
```bash
./deploy.sh bray
./deploy.sh osiris
```

## Verification

```bash
ssh bray "systemctl status apcupsd-exporter"
ssh bray "curl -s http://127.0.0.1:9162/metrics | head"
ssh osiris "curl -s http://127.0.0.1:9162/metrics | head"
```
