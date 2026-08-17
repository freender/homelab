# pve-upgrade

On-demand `apt-get update && apt-get dist-upgrade` for the Debian-based Proxmox
hosts (PVE nodes, PBS, PDM). It never reboots, and it has no timer — running it
is always a deliberate act.

```bash
./deploy --dry-run pve-upgrade ace
./deploy pve-upgrade ace
```

## Why this is manual

Package updates on these hosts are split into two streams that are managed
differently:

| Stream | Origin | How it is applied |
| --- | --- | --- |
| Debian security | `o=Debian, l=Debian-Security` | **Automatic** — `apt-security-updates` |
| Proxmox | `o=Proxmox` | **Manual** — this module, monthly |

`apt-security-updates` installs `unattended-upgrades` scoped so tightly it
cannot see the Proxmox repository, so CVE fixes land on their own and never
need a window. What is left for this module is `pve-manager`, `qemu-server`,
the kernel and ZFS — where the risk is real, a reboot is usually required, and
the decision of *when* is the part that should not be automated.

Do not add a timer to this module. The scripting is trivial; the judgement is
the point.

---

# Monthly rolling upgrade runbook

Written to be delegable. Every step has an explicit command and an explicit
stop condition. **If a check fails, stop and report — do not continue to the
next node.**

## Facts this procedure depends on

Verified 2026-08-16. Re-check rather than trust if anything looks different.

- **Cluster:** `ace`, `bray`, `clovis` — 3 nodes, expected votes 3. `osiris` is
  **standalone** and not part of it.
- **HA is armed:** fencing active, CRM + LRM watchdogs active. HA-managed
  guests are `ct:101` (tower, ace), `ct:104` (arc, bray), `ct:106` (riven,
  bray), `ct:107` (helm, clovis), `ct:108` (neo, bray).
- **`xur` (105) and `deepstone` (111) are not HA-managed** — they live on
  standalone osiris and simply go down with it.
- **`ha: shutdown_policy=migrate`** in `/etc/pve/datacenter.cfg`. Rebooting a
  cluster node **moves its HA guests to another node**; they do not just come
  back locally. All guests are LXC, which cannot live-migrate, so each move is
  a shutdown and restart elsewhere.
- **`crs: ha-rebalance-on-start=1`** — placement may change again after the
  node returns.
- Migration and replication run over VLAN 60 (`10.0.60.0/24`).

Because migration depends on the target node already holding the guest's
dataset, **ZFS replication state is a hard prerequisite for rebooting a cluster
node.** A stale replica is the failure mode that turns a routine reboot into a
restore.

## Order — and why

Do **one node at a time**, in this order:

| # | Node | Guests affected | Why here |
| --- | --- | --- | --- |
| 1 | `osiris` | xur (PBS), deepstone | Standalone: no quorum impact, no HA, no migration. Safest canary. |
| 2 | `bray` | arc, riven, neo | First cluster node, three guests, none user-facing. |
| 3 | `ace` | tower | Largest blast radius: media, storage, primary Traefik/keepalived. |
| 4 | `clovis` | helm | **Last** — helm is the whole monitoring stack. Keep observability through the three riskier nodes; accept one short blind window at the end. |

**Two warnings for whoever runs this:**

- **`riven` is on `bray`.** It hosts the OpenCode server. Step 2 will restart
  it and kill any agent session driving this runbook. Run bray's step from a
  different machine, or expect to reconnect and resume.
- **`clovis` runs the monitoring stack.** During step 4 VictoriaMetrics,
  vmalert, Alertmanager and Grafana are down, so no alert can fire, including
  one about clovis. The `Watchdog` dead-man's switch will go quiet and the
  external check will report it — that is expected, not an incident.

## Per-node procedure

Replace `<node>` throughout. Never run two nodes concurrently.

### 1. Pre-flight (stop if any check fails)

```bash
# Cluster healthy and quorate — skip on osiris, which is standalone
ssh <node> 'pvecm status | grep -E "Quorate|Total votes"'
ssh <node> 'ha-manager status'

# Replication current. Any job with a FAIL state or a long-stale last_sync
# means a migration target may not have the guest's data.
ssh <node> 'pvesr status'

# Storage healthy — never start with a degraded pool
ssh <node> 'zpool status -x'

# On mains power, not battery
ssh <node> 'apcaccess status 2>/dev/null | grep -E "STATUS|TIMELEFT" || true'

# No backup running (check PBS tasks on xur, and PVE tasks on the node)
ssh <node> 'pvesh get /nodes/<node>/tasks --limit 5 --output-format json-pretty | head -40'
```

**Stop if:** not quorate, `ha-manager status` shows anything but `quorum OK` and
active LRMs, any replication job failing, `zpool status -x` is not
`all pools are healthy`, the UPS is on battery, or a backup is in progress.

### 2. Move HA guests off (cluster nodes only — skip on osiris)

```bash
ssh <node> 'ha-manager crm-command node-maintenance enable <node>'

# Watch until no HA service still lists <node>
ssh <node> 'ha-manager status'
```

Doing this explicitly is better than relying on `shutdown_policy=migrate` during
the reboot: it moves the guests while you are watching, and a migration that
fails does so before the host has started shutting down.

**Stop if:** any service is still on `<node>` after a few minutes, or a
migration errors.

### 3. Upgrade

```bash
cd ~/homelab
./deploy --dry-run pve-upgrade <node>
./deploy pve-upgrade <node>
```

Read the output. It prints whether a reboot is required at the end.

**Stop if:** the deploy reports a failed host, or `dist-upgrade` reports held
or broken packages.

### 4. Reboot only if needed

```bash
# Authoritative answer — the metric this repo publishes
ssh helm 'curl -s "localhost:8428/api/v1/query?query=homelab_reboot_required{host=\"<node>\"}"'

# Or read it directly on the node
ssh <node> 'cat /var/lib/prometheus/node-exporter/reboot.prom'
```

If `homelab_reboot_required` is `1`, reboot. If `0`, skip to step 5 — most
months there will be no kernel change and no reboot.

```bash
ssh <node> 'systemctl reboot'
```

### 5. Verify before touching the next node

```bash
ssh <node> 'uptime; uname -r'
ssh <node> 'zpool status -x'
ssh <node> 'pvecm status | grep -E "Quorate|Total votes"'     # cluster nodes
ssh <node> 'ha-manager crm-command node-maintenance disable <node>'   # cluster nodes
ssh <node> 'ha-manager status'
ssh <node> 'pct list'
ssh <node> 'systemctl is-active prometheus-node-exporter'

# Pending reboot cleared, and the node is being scraped again
ssh <node> 'cat /var/lib/prometheus/node-exporter/reboot.prom'
```

Then confirm from the monitoring side that the node is back:

```bash
ssh helm 'curl -s "localhost:8428/api/v1/query?query=up{host=\"<node>\"}"'
```

**Stop if:** guests are not running, a pool is degraded, quorum is not restored,
`homelab_reboot_required` is still `1` after the reboot, or the node is not
being scraped.

Only when all of the above pass, move to the next node.

## Never

- Never reboot two cluster nodes at once — 3 nodes means quorum is lost at two.
- Never reboot with a degraded pool or a failing replication job.
- Never add a timer to this module, and never enable automatic reboots.
- Never run this during the 02:00 or 08:00 maintenance windows (see the
  `scheduled-maintenance` interval in `monitoring-config`), or the alert
  suppression there will hide real problems caused by the upgrade.

## What tells you this is due

You do not need to poll for it:

- **`ProxmoxUpdatesAvailable`** — an `info` alert routed with a 168h repeat, so
  it arrives as **one Telegram message per week** listing every node with
  pending Proxmox packages. That is the prompt to schedule this runbook.
- **`RebootRequired`** — fires 24h after an upgrade leaves a node running an
  older kernel than the one installed. Normally that means step 4 was skipped.
- **`SecurityUpdatesPending`** — should never fire. If it does, it means
  `apt-security-updates` has stopped working on that host; that is a bug in the
  automation, not a reason to run this runbook.
