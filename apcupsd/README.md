# Homelab apcupsd Configuration

Master/slave apcupsd setup for Proxmox cluster with coordinated shutdown.

## Architecture

**bray UPS (APC NS 1500M2):**
- Powers: ace, bray, clovis (entire cluster)
- Role: Master with USB connection
- On battery low: Coordinated cluster shutdown

**xur UPS (APC XS 1000M):**
- Powers: xur PBS only
- Role: Independent master with USB connection
- On battery low: Self-shutdown

## Hosts

| Host   | Role   | Config | Shutdown Behavior |
|--------|--------|--------|-------------------|
| bray   | Master | USB    | Triggers cluster-wide host shutdown |
| ace    | Slave  | Net    | Receives shutdown command from bray |
| clovis | Slave  | Net    | Receives shutdown command from bray |
| xur    | Master | USB    | Independent self-shutdown |

## Host Registry

Per-host settings live in `hosts.conf` using `apcupsd.*` keys.

## Shutdown Sequence (bray UPS)

1. bray enables HA maintenance on bray/ace/clovis
2. bray runs shutdown now on ace and clovis
3. bray runs shutdown now on itself (last)

## Deployment

**Single host:**
```bash
./deploy.sh <hostname>
```

**All hosts:**
```bash
./deploy.sh all
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
- Removes config files and telegram integration
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

**Telegram env file:**
Create `/etc/apcupsd/telegram/telegram.env` on each host:
```bash
TELEGRAM_TOKEN=...
TELEGRAM_CHATID=...
```

**Test Telegram:**
```bash
ssh ace "/etc/apcupsd/telegram/telegram.sh -s 'Test' -d 'Test message'"
```

## Quick Reference

```bash
# UPS status
ssh bray "apcaccess status"              # Local UPS (bray)
ssh xur "apcaccess status"               # Local UPS (xur)
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
