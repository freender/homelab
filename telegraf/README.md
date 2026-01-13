# homelab/telegraf

Minimal Telegraf configuration for collecting CPU package temperature from Proxmox cluster hosts.

**Repository:** `/home/freender/homelab/telegraf` on docker-bray

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
- **Collection interval:** 10 seconds
- Sends to VictoriaMetrics at `victoria-metrics.freender.internal:8428`
- Database: `telegraf`

### Bray-Specific Configuration
Bray has a Samsung 860 EVO M.2 boot drive connected via USB bridge that misreports its device type. A custom Python script collects temperature data directly using `smartctl -d sat` and outputs it in InfluxDB line protocol format.

## Configuration Files

**On cluster hosts:**
- `/etc/telegraf/telegraf.conf` - Main configuration (10s interval, output to VictoriaMetrics)
- `/etc/telegraf/telegraf.d/sensors.conf` - CPU temperature input plugin
- `/etc/telegraf/telegraf.d/mem.conf` - Memory metrics input plugin
- `/etc/telegraf/telegraf.d/apcupsd.conf` - UPS monitoring (existing, preserved)

**In this repo:**
- `telegraf.conf` - Main Telegraf configuration template
- `sensors.conf` - Sensors input plugin configuration
- `mem.conf` - Memory metrics input plugin configuration
- `smartctl.conf` - SMART monitoring input plugin configuration
- `smartctl-bray.conf` - Bray boot disk exec configuration
- `smartctl-bray-boot.py` - Bray boot disk temperature script
- `smartctl-bray-smartctl.conf` - Bray smartctl override (excludes boot disk)
- `diskio.conf` - Disk I/O monitoring configuration
- `net.conf` - Network interface monitoring configuration
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

**What the deployment does:**
1. Installs `telegraf` and `lm-sensors` if not present
2. Configures `lm-sensors` (auto-detect)
3. Deploys main config to `/etc/telegraf/telegraf.conf`
4. Deploys sensors config to `/etc/telegraf/telegraf.d/sensors.conf`
5. Deploys memory config to `/etc/telegraf/telegraf.d/mem.conf`
6. Preserves existing configs in `telegraf.d/` (e.g., apcupsd.conf)
7. Enables and restarts telegraf service

## Verification

Check metrics in VictoriaMetrics:
```bash
curl -s 'http://victoria-metrics.freender.internal:8428/api/v1/query?query=sensors_temp_input' | jq
```

Check service on a host:
```bash
ssh ace "systemctl status telegraf"
ssh ace "journalctl -u telegraf -f"
```

Test configuration:
```bash
ssh ace "telegraf --test --config /etc/telegraf/telegraf.conf"
```

View loaded plugins:
```bash
ssh ace "telegraf --config /etc/telegraf/telegraf.conf --config-directory /etc/telegraf/telegraf.d --test --input-filter sensors"
```

## Metrics Query Examples

Get CPU package temps for all hosts:
```bash
curl -s 'http://victoria-metrics.freender.internal:8428/api/v1/query?query=sensors_temp_input' | \
  jq '.data.result[] | select(.metric.chip | contains("coretemp")) | select(.metric.feature == "package_id_0") | {host: .metric.host, temp: .value[1]}'
```

Get CPU temp history (last hour):
```bash
curl -s 'http://victoria-metrics.freender.internal:8428/api/v1/query_range?query=sensors_temp_input{feature="package_id_0",host="ace"}&start=-1h&step=10s' | jq
```

## Grafana Query

For Grafana dashboards, use this PromQL query:

**CPU Package Temperature:**
```promql
sensors_temp_input{chip="coretemp-isa-0000",feature="package_id_0"}
```

**All Core Temperatures:**
```promql
sensors_temp_input{chip="coretemp-isa-0000",feature=~"core_.*"}
```

**NVMe Temperatures:**
```promql
sensors_temp_input{chip=~"nvme-.*",feature="composite"}
```

**Memory Used Percent:**
```promql
mem_used_percent
```

**Memory Available (GB):**
```promql
mem_available / 1024 / 1024 / 1024
```

**Memory Used (GB):**
```promql
mem_used / 1024 / 1024 / 1024
```

## Prerequisites

The deployment script automatically configures the InfluxData repository if not present:

```bash
# InfluxData repository (automatically added by deploy.sh)
deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main
```

This ensures Telegraf can be installed and updated from the official InfluxData repository.
