# pve-upgrade

On-demand `apt-get update && apt-get dist-upgrade` for the Debian-based Proxmox
hosts (PVE nodes, PBS, PDM). It never reboots, and it has no timer — running it
is always a deliberate act.

```bash
./deploy --dry-run pve-upgrade ace              # ungated: changes nothing
./deploy --confirm-upgrade pve-upgrade ace      # live: dist-upgrades the host
```

A live run **requires `--confirm-upgrade`** and this module is **excluded from
`./deploy all`**. Both exist because this module is unlike every other one here:
its deploy action *is* the mutation. Elsewhere `deploy` converges config and
re-running is a no-op; here it upgrades packages on the target. So "deploy
everything" must not reach it, and naming it explicitly should not be enough
either — sweeping the cluster in registry order with no preflight is precisely
what the runbook below exists to prevent.

Note `--confirm-upgrade` is distinct from `--force`, which everywhere in this
repo means `FORCE_UPDATE=true` (re-copy files that have not changed) and has no
effect on whether packages are upgraded.

## What is automated, and what this runbook is now for

**Upgrades on the four PVE nodes are no longer manual.** `apt-upgrade` runs a
full `apt-get -y dist-upgrade` on each node daily — Proxmox packages, ZFS and
the kernel included — at 05:00, 05:05, 05:10 and 05:15 (osiris, bray, ace,
clovis). `arc` and `xur` are on the same module at 04:05 and 04:00. The
`apt-security-updates` module that previously narrowed the nodes to the
Debian-Security origin has been archived; `apt-upgrade` is now the single apt
mechanism for the fleet.

| Stream | How it is applied |
| --- | --- |
| Debian security | **Automatic** — `apt-upgrade`, daily |
| Proxmox, ZFS, kernel | **Automatic** — `apt-upgrade`, daily |
| **Reboot into a new kernel** | **Manual** — this runbook |

What was never automated, and still is not, is the reboot. Installing a kernel
is cheap and reversible; booting into it on a node whose guests are all LXC and
cannot live-migrate is neither. With `ha shutdown_policy=migrate`, entering HA
maintenance restarts every guest on the node twice. That decision — *when*, and
in *what order* — is the judgement this runbook exists to hold.

The prompt to run it is the Saturday 09:00 `RebootRequired` Telegram digest.
Nothing else will tell you.

This module still deploys to `arc` and `xur` as an on-demand "upgrade now"
escape hatch, and remains `include_in_all=False` behind `--confirm-upgrade`.

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

- **`riven` is on `bray`.** It hosts the OpenCode server *and* the shared SSH
  agent. If bray needs a reboot, step 3a restarts riven, which kills any agent
  session driving this runbook and empties the SSH agent — expect to reconnect
  and re-run `addhomelabkeys`, or run bray's step from a different machine. If
  bray needs no reboot, none of this happens, which is the main reason step 3a
  is gated on the reboot check rather than run up front.
- **`clovis` runs the monitoring stack.** If clovis reboots, VictoriaMetrics,
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

### 2. Upgrade

```bash
cd ~/homelab
./deploy --dry-run pve-upgrade <node>
./deploy --confirm-upgrade pve-upgrade <node>
```

Read the output. It prints whether a reboot is required at the end.

**Stop if:** the deploy reports a failed host, or `dist-upgrade` reports held
or broken packages.

### 3. Reboot only if needed

```bash
# Authoritative answer — the metric this repo publishes
ssh <node> 'cat /var/lib/prometheus/node-exporter/reboot.prom'

# Or from the monitoring side (note the shell-quoting: use single quotes inside
# the label selector, or the outer shell eats the escaping)
ssh helm "curl -s --get localhost:8428/api/v1/query \
  --data-urlencode 'query=homelab_reboot_required{host=\"<node>\"}'"
```

If `homelab_reboot_required` is `0`, **skip the rest of this step entirely** and
go to step 4. Most months there is no kernel change and no reboot, and in that
case the node's guests must not be touched at all.

If it is `1`, **stop and hand off to a human.** Everything below in this step —
the migration and the reboot both — is a manual decision, never performed by a
delegated or agent-driven run, no matter how healthy the pre-flight looked. An
automated run ends here: report that the node needs a reboot, quote the commands
below, and leave the node up and unmigrated. The guests are only worth
restarting once someone has decided to take the reboot, so the migration is part
of the reboot, not a prerequisite to be staged in advance.

The rest of this step is the human's reference procedure.

#### 3a. Move HA guests off (cluster nodes only — skip on osiris)

```bash
ssh <node> 'ha-manager crm-command node-maintenance enable <node>'

# Watch until no HA service still lists <node>
ssh <node> 'ha-manager status'
```

Doing this explicitly is better than relying on `shutdown_policy=migrate` during
the reboot: it moves the guests while you are watching, and a migration that
fails does so before the host has started shutting down.

**This is why it is gated on the reboot check.** All guests are LXC and cannot
live-migrate, so entering maintenance shuts down and restarts every HA guest on
the node — and because `crs: ha-rebalance-on-start=1`, leaving maintenance in
step 4 restarts them a *second* time when they return. Two restarts per guest is
an acceptable price for a kernel reboot and a pure loss without one. That
asymmetry is also why the whole step is manual: the cost lands on running
services, so a human takes it deliberately or not at all.

**Stop if:** any service is still on `<node>` after a few minutes, or a
migration errors.

#### 3b. Reboot

```bash
ssh <node> 'systemctl reboot'
```

### 4. Verify before touching the next node

```bash
ssh <node> 'uptime; uname -r'
ssh <node> 'zpool status -x'
ssh <node> 'pvecm status | grep -E "Quorate|Total votes"'     # cluster nodes
ssh <node> 'ha-manager status'
ssh <node> 'pct list'
ssh <node> 'systemctl is-active prometheus-node-exporter'

# Pending reboot cleared, and the node is being scraped again
ssh <node> 'cat /var/lib/prometheus/node-exporter/reboot.prom'
```

**Only if you entered maintenance in step 3a**, leave it — this is what pulls the
guests back and restarts them a second time, so do not run it on a node you never
put into maintenance:

```bash
ssh <node> 'ha-manager crm-command node-maintenance disable <node>'   # cluster nodes
ssh <node> 'ha-manager status'
```

Then confirm from the monitoring side that the node is back:

```bash
ssh helm "curl -s --get localhost:8428/api/v1/query \
  --data-urlencode 'query=up{host=\"<node>\"}'"
```

**Stop if:** guests are not running, a pool is degraded, quorum is not restored,
`homelab_reboot_required` is still `1` after the reboot, or the node is not
being scraped.

Only when all of the above pass, move to the next node.

## Never

- **Never reboot as part of an automated or delegated run. The reboot is always a
  manual step, taken by a human, every time.** Step 3 stops at the reboot check
  and hands off; a green pre-flight is not authorization to proceed.
- Never enter HA maintenance on a node you are not about to reboot — it is part
  of the reboot, not a preparation for the upgrade.
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
- **`RebootRequired`** — fires 1h after an upgrade leaves a node running an
  older kernel than the one installed, and is delivered as a single collapsed
  Telegram message in the Saturday 09:00–09:10 window. **This is now the primary
  trigger for this runbook.** The 1h `for:` is deliberately short and coupled to
  the 05:00–05:15 upgrade band: a kernel installed then must cross the threshold
  before 09:00 or the prompt is held a further week. Do not move either without
  the other.
- **`SecurityUpdatesPending`** / **`ProxmoxUpdatesAvailable`** — should now both
  be silent. Either firing means `apt-upgrade`'s daily dist-upgrade has stopped
  achieving anything on that host, which is a bug in the automation, not a
  reason to run this runbook. The faster signal for a hard failure is
  `SystemdUnitFailed` on `homelab-apt-dist-upgrade.service`.
