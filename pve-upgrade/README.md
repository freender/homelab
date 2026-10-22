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
full `apt-get -y dist-upgrade` on each node every **Saturday** — Proxmox
packages, ZFS and the kernel included — at 05:00, 05:05, 05:10 and 05:15
(osiris, bray, ace, clovis), a few hours before the 09:00 `RebootRequired`
digest. In between, `apt-upgrade.security_updates` scopes the stock
unattended-upgrades to Debian-Security, so Debian security fixes still install
daily (06:00–07:00). Proxmox has no security suite, so an urgent Proxmox CVE
is one case for running `./deploy --confirm-upgrade pve-upgrade <node>`
mid-week; the other is a kernel released since Saturday, ahead of a reboot
(runbook step 2). `arc` and `xur` are on the same module, daily at 04:05 and
04:00.

| Stream | How it is applied |
| --- | --- |
| Debian security | **Automatic** — unattended-upgrades, daily |
| Proxmox, ZFS, kernel | **Automatic** — `apt-upgrade`, Saturday |
| **Reboot into a new kernel** | **Manual** — this runbook |

What was never automated, and still is not, is the reboot. Installing a kernel
is cheap and reversible; booting into it on a node that carries LXC guests,
which cannot live-migrate, is neither. With `ha shutdown_policy=migrate`,
entering HA maintenance restarts every LXC on the node twice. That decision —
*when*, and in *what order* — is the judgement this runbook exists to hold.

The prompt to run it is the Saturday 09:00 `RebootRequired` Telegram digest.
Nothing else will tell you.

This module still deploys to `arc` and `xur` as an on-demand "upgrade now"
escape hatch, and remains `include_in_all=False` behind `--confirm-upgrade`.

Do not add a timer to this module. The scripting is trivial; the judgement is
the point.

---

# Rolling reboot runbook

Driven by `/roll`. Written to be delegable. Every step has an explicit
command and an explicit stop condition. **If a check fails, stop and report — do
not continue to the next node.**

**`./roll` runs the checks.** `./roll plan [host...]` performs steps 1–3 below
for every node and own-kernel VM at once, applies the stop conditions, and
prints the wave order; `./roll verify [--wait N] <host>...` performs step 4.
Neither reboots anything. The stop conditions are enforced in
`src/homelab/roll/checks.py`; the commands in each step are what it reads,
kept here as the manual fallback for when `./roll` itself is broken.

```bash
./roll plan                    # verdict: nothing-pending | ready | stop
./roll verify --wait 900 ace   # after ace's reboot; exits 0 once it has recovered
```

**The human is asked once.** `/roll` pre-flights every in-scope node first,
then presents one plan covering the whole roll; approving it authorizes every
reboot in it. The steps below are therefore surveyed for all nodes before any
reboot happens, and re-run per wave only as the verification in step 4. Further
prompting is reserved for a plan that has stopped matching reality — not for
each wave in turn.

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
  guests are `ct:101` (tower, ace), `ct:104` (arc, bray), `ct:108` (neo,
  bray), and the VMs `vm:109` (zion, ace), `vm:110` (riven, bray) and
  `vm:112` (helm, clovis).
- **`xur` (105) and `deepstone` (111) are not HA-managed** — they live on
  standalone osiris and simply go down with it.
- **`ha: shutdown_policy=migrate`** in `/etc/pve/datacenter.cfg`. Rebooting a
  cluster node **moves its HA guests to another node**; they do not just come
  back locally. LXC guests cannot live-migrate, so each LXC move is a shutdown
  and restart elsewhere. The three VMs, zion, riven and helm, **live-migrate**: their
  replica on the target is topped up incrementally and RAM is copied with
  ~100 ms downtime (~13 s end to end for zion), so the OpenCode session and the
  shared SSH agent on riven survive the move (freender/homelab-ops#125).
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
The first wave is chosen at run time from tower's current HA placement, not from
an inventory assumption:

```bash
ssh ace 'ha-manager status | grep "ct:101"' # ct:101 is tower
```

| Wave | Node(s) | Guests | Why here |
| --- | --- | --- | --- |
| 1 | `osiris` **+** the non-tower node (`ace` or `clovis`) | xur (PBS), deepstone / HA guests on that selected node | osiris holds no vote, so this is one cluster node down, not two — cluster stays 2/3. Pair osiris with the node that does **not** currently carry tower, leaving tower's node for wave 2. |
| 2 | Tower's current node (`ace` or `clovis`) | tower / other guests on that selected node | Only after wave 1 is fully back and HA has finished rebalancing. If this is clovis, helm live-migrates with it. |
| 3 | `bray` | arc, riven, neo | **Last by preference** (see below). |

Concretely: tower on **ace** means `osiris` + `clovis`, then `ace`, then
`bray`; tower on **clovis** means `osiris` + `ace`, then `clovis`, then `bray`.
If tower is already on bray, retain the latter order: bray stays last by
preference because it hosts riven and the OpenCode session.

**Why bray is last — preferred, not required.** `riven` runs on bray, and riven
is the OpenCode server *and* the shared SSH agent. Rebooting bray live-migrates
riven to ace, and that is proven: on 2026-10-12 an HA round trip bray -> ace ->
bray took 96 ms and 76 ms of downtime with the session, agent, Syncthing and both runners
intact (homelab-ops#123). Proven is still not guaranteed: if a migration fails,
HA restarts riven from its replica and the session and agent are gone. Doing
bray last means every other node is already verified when that residual risk is
taken, so it stays the default order. When the roll has a reason to put bray
earlier, that is acceptable now; it is no longer a hard rule.

By the same logic, clovis comes *before* bray, not last. helm (VM 112) is the
whole monitoring stack, and a clovis reboot live-migrates it to ace, so
monitoring normally sees only a sub-second pause. If that migration fails, HA
restarts helm from its replica and VictoriaMetrics, vmalert, Alertmanager and
Grafana are down until it is back: no alert can fire, including one about
clovis, and the `Watchdog` dead-man's switch goes quiet. Taking clovis before
bray means that failure, if it happens, is found and recovered before the one
reboot that risks the agent.

**Two things HA does not cover:**

- **osiris has no HA at all.** It is standalone, so xur (your primary PBS) and
  deepstone simply go down with it and come back on boot — nothing migrates.
  Confirm no backup or sync is running before you take it.
- **`void` on clovis is not an HA resource** (`ha-manager status` lists only
  ct:101, 104, 108 and vm:109, 110, 112). It will not migrate, and it has no
  `onboot`, so it stays stopped after clovis returns and verify does not
  expect it back.

## Per-wave procedure

Replace `<node>` throughout. Wave 1 may reboot `osiris` and the selected
non-tower node (`ace` or `clovis`) together; they are deliberately paired
because osiris is not a cluster voter. Every other wave has one node. Never
reboot two cluster nodes concurrently.

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

# The node can actually boot. Every other check on this list was green on ace
# on 2026-09-19 while its NVRAM pointed at an unbootable entry, and it did not
# come back from the reboot. 0 dead entries and a loadable first entry.
ssh <node> 'grep -v "^#" /var/lib/prometheus/node-exporter/boot-entries.prom'

# On mains power, not battery
ssh <node> 'apcaccess status 2>/dev/null | grep -E "STATUS|TIMELEFT" || true'

# No backup running (check PBS tasks on xur, and PVE tasks on the node)
ssh <node> 'pvesh get /nodes/<node>/tasks --limit 5 --output-format json-pretty | head -40'
```

**Stop if:** not quorate, `ha-manager status` shows anything but `quorum OK` and
active LRMs, any replication job failing, `zpool status -x` is not
`all pools are healthy`, the UPS is on battery, a backup is in progress, or
`homelab_boot_first_entry_loadable` is `0` (the node will not come back — fix
the boot entry with `efibootmgr` first, while it is still reachable) or
`homelab_boot_entries_dead` is above `0` (clean the husks before rebooting, or
the node is one NVRAM shuffle from the same fate).

### 2. Confirm the automation is current

Do **not** run `./deploy --confirm-upgrade pve-upgrade <node>` once the roll has
started. `apt-upgrade` owns these nodes; dist-upgrading one mid-runbook
introduces the unreviewed package change the ordering exists to prevent. This
step only reads state:

```bash
# The Saturday upgrade ran and succeeded
ssh <node> 'systemctl status homelab-apt-dist-upgrade.timer --no-pager | head -4'
ssh <node> 'systemctl show homelab-apt-dist-upgrade.service -p Result -p ExecMainStatus'

# Nothing left pending, and no kernel among it
ssh <node> "apt-get -s -o DPkg::Lock::Timeout=600 dist-upgrade | grep -E '^Inst |upgraded,'"
```

**Stop if:** the service's last `Result` is anything but `success` — the
automation is failing; fix that first (`SystemdUnitFailed` covers it).

**Also stop if a newer kernel is available but not installed** — typically one
Proxmox or Debian released after Saturday's run. Rebooting now boots the
installed kernel and owes a second reboot once the new one lands. Install it
first, then plan again: `./deploy --confirm-upgrade pve-upgrade <node>` on a
node, or `systemctl start homelab-apt-dist-upgrade.service` as root on a VM.
That is the one sanctioned use of `pve-upgrade` here: *before* a roll starts,
never between its waves. The same reading on a node with nothing pending is a
warning — the next upgrade will need a reboot.

Other pending packages only warn: they do not change what the reboot boots
into. Package lists older than 48 h warn too, because they can hide a new
kernel from this check.

### 3. Reboot only if needed

```bash
# Authoritative answer — the metric this repo publishes
ssh <node> 'cat /var/lib/prometheus/node-exporter/reboot.prom'

# Or from the monitoring side (note the shell-quoting: use single quotes inside
# the label selector, or the outer shell eats the escaping)
ssh helm "curl -s --get localhost:8428/api/v1/query \
  --data-urlencode 'query=homelab_reboot_required{host=\"<node>\"}'"
```

If `homelab_reboot_required` is `0`, the node **drops out of the plan entirely**
— no reboot, and its guests are not touched. Most months that is every node, and
the whole run ends here with "nothing pending".

If it is `1`, the node joins the plan. `/roll` may reboot it **only after
the human has approved the plan through the `question` tool** — a single approval
covering the full roll, raised once after every node has been surveyed, not one
per wave. The prompt must name each node needing a reboot in wave order, the
guests it moves or stops, running vs installed kernels, and that the run ends on
`bray`, which live-migrates riven and puts the session at risk. Approval is the authorization for every reboot
listed; a green pre-flight is not, and no persistent auto-reboot setting exists.

**Do not enter HA maintenance.** Cluster nodes have
`ha: shutdown_policy=migrate`, so a direct reboot hands HA services off on its
own. Entering maintenance is worse here: tower, arc and neo are LXC and cannot
live-migrate, so it restarts each of them during the drain and, with
`crs: ha-rebalance-on-start=1`, restarts them again when the node returns.

#### 3a. Reboot after confirmation

```bash
ssh <node> 'systemctl reboot'
```

For wave 1, issue the reboots for `osiris` and the selected non-tower node;
osiris is standalone, so only one of the three cluster votes is down. Do not send
the tower node or bray reboot until this wave is fully recovered — the sequencing
is enforced by step 4's verification, not by asking again. If clovis is a
selected node, its reboot live-migrates helm to ace: confirm
`ssh ace 'ha-manager status | grep vm:112'` shows it started on ace, and that
the step 4 queries against helm answer, before continuing. For bray, the reboot
live-migrates riven, and with it the OpenCode session running this workflow, to
ace. Verify the migration first (`ssh ace 'ha-manager status | grep vm:110'`
shows it started on ace), then run step 4 for bray as for any other node. If the session does not survive, the
human opens a new session, re-runs `addhomelabkeys`, and verifies from there.

### 4. Verify before touching the next node

```bash
ssh <node> 'uptime; uname -r'
ssh <node> 'zpool status -x'
ssh <node> 'pvecm status | grep -E "Quorate|Total votes"'     # cluster nodes
ssh <node> 'ha-manager status'
ssh <node> 'pct list; qm list'                      # every onboot guest HA does not manage: running
ssh <node> 'grep -l "^onboot: 1" /etc/pve/local/{lxc,qemu-server}/*.conf'
ssh <node> 'systemctl is-active prometheus-node-exporter'
ssh <node> 'systemctl --failed --no-legend'

# Every enabled service is active again. Of what this prints, oneshots, units
# skipped by a condition (ConditionResult=no) and socket-activated units are exempt.
ssh <node> 'for u in $(systemctl list-unit-files --type=service --state=enabled --no-legend | awk "{print \$1}" | grep -v "@[.]"); do systemctl is-active -q "$u" || echo "$u"; done'

# Pending reboot cleared: `uname -r` must equal the `installed` label. The
# `homelab_reboot_required` flag itself is only refreshed 2 min after boot, so
# right after a reboot it still reads 1.
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

**Stop if:** an onboot guest outside HA (on osiris, xur and deepstone) is not
running, a unit failed or an enabled service is not active, a pool is degraded,
quorum is not restored,
`homelab_reboot_required` is still `1` after the reboot, the node is not
being scraped, or helm cannot answer the query (no answer is not a pass).

Only when all of the above pass for every node in the wave, move to the next
wave — directly, on the approval already given. Re-prompt the human only if the
roll must deviate from the approved plan: a node that has newly become pending,
tower having moved such that the wave order is now wrong, or a recovered-but-
degraded state where continuing is a judgement call. A clean wave is not a
decision point.

## Never

- **Never reboot without an explicit approval of the plan through the `question`
  tool.** A green pre-flight is not authorization. The approval is the human
  decision to take the outage; without it, stop and leave every node up. It is
  asked once for the whole roll — never reboot a node that was not named in the
  plan that was approved.
- **Never enter HA maintenance.** `ha: shutdown_policy=migrate` handles HA
  services during the direct reboot. Maintenance would restart every LXC twice.
- Never reboot two cluster nodes at once — 3 nodes means quorum is lost at two.
- Never reboot with a degraded pool or a failing replication job.
- Never add a timer to this module, and never enable automatic reboots.
- Never run this during the 02:00 or 08:00 maintenance windows (see the
  `scheduled-maintenance` interval in `monitoring-config`), or the alert
  suppression there will hide real problems caused by the reboot.
- Never run `./deploy --confirm-upgrade pve-upgrade <node>` once a roll has
  started. `apt-upgrade` owns these nodes; forcing an off-schedule dist-upgrade
  mid-roll is the unreviewed package change the ordering exists to prevent. The
  only exception is before the roll, when `./roll plan` stops on a kernel that
  is available but not installed (step 2).

## What tells you this is due

You do not need to poll for it:

- **`RebootRequired`** — fires 1h after an upgrade leaves a node running an
  older kernel than the one installed, and is delivered as a single collapsed
  Telegram message in the Saturday 09:00–09:10 window. **This is now the primary
  trigger for this runbook.** The 1h `for:` is deliberately short and coupled to
  the Saturday 05:00–05:15 upgrade band: a kernel installed then must cross the
  threshold before 09:00 or the prompt is held a further week. Do not move
  either without the other.
- **`SecurityUpdatesPending`** / **`ProxmoxUpdatesAvailable`** — should now both
  be silent. Either firing means the daily security run or the Saturday
  dist-upgrade has stopped
  achieving anything on that host, which is a bug in the automation, not a
  reason to run this runbook. The faster signal for a hard failure is
  `SystemdUnitFailed` on `homelab-apt-dist-upgrade.service`.

---

# Rebooting riven

`riven` (VM 110) boots its own kernel, and `apt-upgrade` installs new ones
there daily at 08:03 without rebooting. A bray reboot **live-migrates** riven
and does not reboot it, so the rolling runbook above never clears a pending
riven kernel. riven reports `homelab_reboot_required` like a bare-metal node
(`metrics-exporters.own_kernel`), so `RebootRequired` names it in the same
Saturday digest (freender/homelab-ops#134).

riven is the OpenCode server and holds the shared SSH agent, so rebooting it
ends the session that issues the reboot. The procedure is built around that.

**Who:** the agent, only after the human approves it through the `question`
tool. `/roll` surveys riven with the nodes and puts it in the same single
approval, as the very last step of the run. Outside `/roll`, ask on its
own. If a roll is also due, finish the roll first, because riven's reboot ends
the session that drives it. Never inside the 02:00 or 08:00 maintenance windows.

### Pre-flight

```bash
cat /var/lib/prometheus/node-exporter/reboot.prom   # homelab_reboot_required 1
systemctl --user is-active github-runner github-runner-apply
gh run list --repo freender/homelab-ops --status in_progress --limit 5
```

**Stop if** a workflow is running on a riven runner. Wait for it to finish,
because the reboot would kill the job.

### Reboot (detached, delayed)

freender has no passwordless sudo, so the reboot goes through root SSH. It is
also scheduled a minute out, so the agent can finish its reply first:

```bash
ssh riven-root 'systemd-run --on-active=1min --unit=riven-reboot systemctl reboot'
# cancel within the minute: ssh riven-root 'systemctl stop riven-reboot.timer'
```

Reboot from inside the guest, not with `qm reboot` or `ha-manager`. The QEMU
process keeps running, so HA sees nothing and does not move the VM.

### After the reboot

The human reconnects and re-runs `addhomelabkeys`, because the agent is gone.
The next session then runs `./roll verify riven`. It requires every enabled
service to be active, system and freender's `--user` alike (freender lingers,
so its units come back with the host), with no unit list kept anywhere.
The manual equivalent:

```bash
uname -r                                            # the newest installed kernel
cat /var/lib/prometheus/node-exporter/reboot.prom   # homelab_reboot_required 0
systemctl is-active ssh-agent-freender syncthing-riven
systemctl --user is-active opencode github-runner github-runner-apply
systemctl --failed --no-legend; systemctl --user --failed --no-legend
```

**Stop and report if** any enabled service is not active, the user manager is
not running, or the kernel did not change.

---

# Rebooting helm

`helm` (VM 112) boots its own kernel, and `apt-upgrade` installs new ones there
daily at 08:01 without rebooting. A clovis reboot **live-migrates** helm and
does not reboot it, so the rolling runbook above never clears a pending helm
kernel. helm reports `homelab_reboot_required` (`metrics-exporters.own_kernel`),
so `RebootRequired` names it in the Saturday digest like riven.

helm is the whole monitoring stack (VictoriaMetrics, vmalert, Alertmanager,
Grafana), the secondary keepalived node for the Traefik VIP `10.0.40.15`, and
one of the three Docker Swarm managers. Rebooting it opens a monitoring blind
window of a minute or two: no alert can fire, and the `Watchdog` heartbeat
pauses. Unlike riven, it does not end the session that issues it.

**Who:** the agent, only after the human approves it through the `question`
tool. `/roll` includes helm in its single approval and reboots it after
every node wave has verified. Never in the middle of a node roll, and never
inside the 02:00 or 08:00 maintenance windows.

### Pre-flight

```bash
ssh helm 'cat /var/lib/prometheus/node-exporter/reboot.prom'   # homelab_reboot_required 1
ssh tower 'docker node ls'           # the other two managers Ready / Reachable
ssh helm 'ip -br addr show nic0'     # whether helm holds the VIP right now
```

**Stop if** tower or neo is not a healthy manager: with helm down, swarm
quorum needs both. If helm holds `10.0.40.15`, the VIP fails over to tower
(priority 101) or neo; that is expected, not a reason to stop.

### Reboot

Reboot from inside the guest, not with `qm reboot` or `ha-manager`, so HA sees
nothing and does not move the VM:

```bash
ssh helm-root 'systemctl reboot'
```

### After the reboot

```bash
ssh helm 'uname -r'                                            # the newest installed kernel
ssh helm 'cat /var/lib/prometheus/node-exporter/reboot.prom'   # homelab_reboot_required 0
ssh helm 'systemctl --failed --no-legend'          # and every enabled service active (step 4)
ssh helm 'docker ps -a --format "{{.Names}} {{.Status}}" | grep -v " Up " || true'
ssh helm 'curl -sf localhost:9093/-/ready && curl -sf localhost:8428/health'
ssh tower 'docker node ls'           # helm back as a Reachable or Leader manager
```

**Stop and report if** a unit failed or an enabled service is not active, a
container is not up, Alertmanager or VictoriaMetrics is not ready, helm has not rejoined the swarm, or the kernel did not change.
