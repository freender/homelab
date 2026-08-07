# Homelab apcupsd Configuration

Master/slave apcupsd setup for Proxmox cluster with coordinated shutdown.

## Architecture

**bray UPS (APC NS 1500M2):**
- Powers: ace, bray, clovis (entire cluster)
- Role: Master with USB connection
- On battery low: Coordinated cluster shutdown

**osiris UPS (APC XS 1000M):**
- Powers: osiris PVE host only
- Role: Independent master with USB connection
- On battery low: Self-shutdown

## Hosts

| Host   | Role   | Config | Shutdown Behavior |
|--------|--------|--------|-------------------|
| bray   | Master | USB    | Triggers cluster-wide host shutdown |
| ace    | Slave  | Net    | Receives shutdown command from bray |
| clovis | Slave  | Net    | Receives shutdown command from bray |
| osiris | Master | USB    | Independent self-shutdown |

## Host Registry

Per-host settings live in `hosts.conf` using `apcupsd.*` keys.

## Shutdown Sequence (bray UPS)

1. bray, ace, or clovis disarms HA with `ignore` mode before stopping any guest.
2. Proxmox's normal system shutdown stops each node's local guests without HA restarts or migrations.
3. bray powers off ace and clovis, then itself; a slave event also schedules that node's local poweroff.
4. On power return, `homelab-ha-rearm.service` waits for cluster quorum and re-arms HA.
   Managed resources retain their `started` desired state and recover automatically.

## Deployment

**Single host:**
```bash
cd ~/homelab && ./deploy apcupsd <hostname>
```

**All hosts:**
```bash
cd ~/homelab && ./deploy apcupsd all
```

## Removal

**Single host:**
```bash
./remove.sh <hostname>
```

**All hosts:**
```bash
./remove.sh all
```

**Purge package:**
```bash
./remove.sh --purge all
```

**Skip confirmation:**
```bash
./remove.sh --yes all
```

**What it does:**
- Stops and disables the apcupsd service
- Backs up `/etc/apcupsd/` to `/etc/apcupsd.bak.TIMESTAMP`
- Removes config files (including the retired telegram integration, if present)
- Resets `/etc/default/apcupsd` (ISCONFIGURED=no)
- Optionally purges the apcupsd package with `--purge`

## Testing

**Dry-run (no actual shutdown):**
```bash
./scripts/test-shutdown.sh
```

**Verify NIS communication:**
```bash
ssh ace "apcaccess status | grep STATUS"
ssh clovis "apcaccess status | grep STATUS"
```

**UPS alerting:**
This module no longer sends Telegram messages. UPS state is exported by
`apcupsd-exporter` (deployed by `metrics-exporters` on the UPS master hosts) and
alerted on by the `ups` rule group in vmalert on helm. To check what the
alerting stack currently sees:
```bash
ssh bray "curl -s localhost:9162/metrics | grep -E '^apcupsd_(status|up|time_left)'"
```

## Quick Reference

```bash
# UPS status
ssh bray "apcaccess status"              # Local UPS (bray)
ssh osiris "apcaccess status"            # Local UPS (osiris)
ssh ace "apcaccess status"  # Slave view (ace)
ssh clovis "apcaccess status"  # Slave view (clovis)

# Service management
systemctl status apcupsd
systemctl restart apcupsd

# Logs
journalctl -u apcupsd -f
journalctl -t apcupsd-shutdown

# Event log
tail -f /var/log/apcupsd.events
```
