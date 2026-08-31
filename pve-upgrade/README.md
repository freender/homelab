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

# Rolling reboot runbook

Driven by `/pve-reboot`. Written to be delegable. Every step has an explicit
command and an explicit stop condition. **If a check fails, stop and report — do
not continue to the next node.**

This runbook no longer upgrades anything. `apt-upgrade` has already installed
the packages by the time you get here; what remains is deciding whether a node
owes a reboot, and taking it in an order that never leaves the cluster short of
quorum or a migration target. Most weeks the correct outcome is "nothing
pending" — say so rather than finding work.

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

Only **ace, bray and clovis** are in the cluster (`net-cluster`, 3 votes, quorum
at 2). **`osiris` is standalone** — `pvecm status` fails there by design. That is
what makes wave 1 below safe: taking osiris down costs no vote.

`ha: shutdown_policy=migrate` is set, so a plain `reboot` on a cluster node hands
its HA services off by itself. You do not have to enter maintenance mode first.

| Wave | Node(s) | Guests | Why here |
| --- | --- | --- | --- |
| 1 | `osiris` **+** `ace` | xur (PBS), deepstone / tower | osiris holds no vote, so this is one cluster node down, not two — cluster stays 2/3. ace has the largest blast radius (media, storage, primary Traefik/keepalived), so it goes while every tool you might need is still up. |
| 2 | `clovis` | helm, `void` (VM) | Only after wave 1 is fully back and HA has finished rebalancing. |
| 3 | `bray` | arc, riven, neo | **Last, and this is the point of the order.** |

**Why bray is last.** `riven` runs on bray, and riven is the OpenCode server *and*
the shared SSH agent. Rebooting bray kills the session driving this runbook and
empties the agent. Doing it last means every other node is already verified, so
losing your tooling costs nothing — you reconnect, re-run `addhomelabkeys`, and
the roll is already done. An order that put bray in the middle would drop the
agent with two nodes still to go, which is why the earlier
`osiris, bray, ace, clovis` ordering was wrong for a delegated run.

By the same logic, clovis (wave 2) comes *before* bray, not last: helm is the
whole monitoring stack, and finishing it in wave 2 means observability is back
up before the one reboot you take with no agent. Accept the blind window during
clovis itself — VictoriaMetrics, vmalert, Alertmanager and Grafana are all down,
so no alert can fire, including one about clovis. The `Watchdog` dead-man's
switch going quiet is expected, not an incident.

**Two things HA does not cover:**

- **osiris has no HA at all.** It is standalone, so xur (your primary PBS) and
  deepstone simply go down with it and come back on boot — nothing migrates.
  Confirm no backup or sync is running before you take it.
- **`void` on clovis is not an HA resource** (`ha-manager status` lists only
  ct:101, 104, 106, 107, 108). It will not migrate; it stops with clovis and
  returns on boot.

## Per-wave procedure

Replace `<node>` throughout. Wave 1 may reboot `osiris` and `ace` together;
they are deliberately paired because osiris is not a cluster voter. Every
other wave has one node. Never reboot two cluster nodes concurrently.

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

### 2. Confirm the automation is current

Do **not** run `./deploy --confirm-upgrade pve-upgrade <node>` here. `apt-upgrade`
owns these nodes; dist-upgrading one mid-runbook introduces the unreviewed
package change the ordering exists to prevent. This step only reads state:

```bash
# The daily upgrade ran and succeeded
ssh <node> 'systemctl status homelab-apt-dist-upgrade.timer --no-pager | head -4'
ssh <node> 'systemctl show homelab-apt-dist-upgrade.service -p Result -p ExecMainStatus'

# Nothing left pending
ssh <node> 'apt-get -s -o DPkg::Lock::Timeout=600 dist-upgrade | tail -2'
```

**Stop if:** the service's last `Result` is anything but `success`, or packages
are still pending — that means the automation is failing and the reboot you are
about to take will not land on the kernel you think it will. Fix that first;
`SystemdUnitFailed` and `ProxmoxUpdatesAvailable` both cover this condition.

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

If it is `1`, `/pve-reboot` may reboot the node **only after it asks the human
to confirm the current wave through the `question` tool.** The prompt must name
answer is. No persistent auto-reboot setting exists.

**Do not enter HA maintenance.** Cluster nodes have
`ha: shutdown_policy=migrate`, so a direct reboot hands HA services off on its
own. Entering maintenance is worse here: all HA guests are LXC and cannot
live-migrate, so it restarts every one during the drain and, with
`crs: ha-rebalance-on-start=1`, restarts them again when the node returns.

#### 3a. Reboot after confirmation

```bash
ssh <node> 'systemctl reboot'
```

For wave 1, issue the confirmed reboots for `osiris` and `ace`; osiris is
standalone, so only one of the three cluster votes is down. Do not send the
clovis or bray reboot until this wave is fully recovered. For clovis, accept the
monitoring blind window and wait for helm to return before continuing. For bray,
the confirmed reboot terminates the OpenCode session running this workflow;
stop after issuing it and have the human open a new session for verification.

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

For cluster nodes, wait until `ha-manager status` is clean and no service is
stuck in transition. `crs: ha-rebalance-on-start=1` may move services back after
the node returns; do not start the next wave while that is happening.

Then confirm from the monitoring side that every node in the completed wave is
back:

```bash
ssh helm "curl -s --get localhost:8428/api/v1/query \
  --data-urlencode 'query=up{host=\"<node>\"}'"
```

**Stop if:** guests are not running, a pool is degraded, quorum is not restored,
`homelab_reboot_required` is still `1` after the reboot, or the node is not
being scraped.

Only when all of the above pass for every node in the wave, move to the next
wave.

## Never

- **Never reboot without an explicit confirmation for that wave through the
  `question` tool.** A green pre-flight is not authorization. The confirmation
  is the human decision to take the outage; without it, stop and leave every
  node up.
- **Never enter HA maintenance.** `ha: shutdown_policy=migrate` handles HA
  services during the direct reboot. Maintenance would restart every LXC twice.
- Never reboot two cluster nodes at once — 3 nodes means quorum is lost at two.
- Never reboot with a degraded pool or a failing replication job.
- Never add a timer to this module, and never enable automatic reboots.
- Never run this during the 02:00 or 08:00 maintenance windows (see the
  `scheduled-maintenance` interval in `monitoring-config`), or the alert
  suppression there will hide real problems caused by the reboot.
- Never run `./deploy --confirm-upgrade pve-upgrade <node>` as a step of this
  runbook. `apt-upgrade` owns these nodes; forcing an off-schedule dist-upgrade
  mid-roll is the unreviewed package change the ordering exists to prevent.

## What tells you this is due

You do not need to poll for it:

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
