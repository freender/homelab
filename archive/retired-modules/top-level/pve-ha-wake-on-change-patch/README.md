# PVE HA Wake-On-Change Patch (never shipped — fenced the cluster)

> **Do not deploy this. It self-fenced every node it ran on.**
>
> Live on ace, bray and clovis for 14 minutes on 2026-11-04 (01:10-01:28 EST) and
> caused **six hard resets**: bray at 01:12:43 (29s after going live), 01:15:54 and
> 01:18:45; clovis at 01:20:07 and 01:28:00; ace at 01:22:50. Reverted to stock
> `pve-ha-manager` 5.2.5 on all three nodes; no reset before or since. No data loss
> — ct:104 (arc) needed an HA error-state recovery (disable/enable), everything
> else came back on its own.
>
> ## Root cause: sub-second HA rounds starve watchdog-mux
>
> `watchdog-mux` only pets `/dev/watchdog` on an **epoll timeout** iteration:
>
> ```c
> nfds = epoll_wait(epollfd, events, MAX_EVENTS, 1000);
> if (nfds == 0 && update_watchdog) {   // timeout
>     ...
>     if (update_watchdog) { ioctl(watchdog_fd, WDIOC_KEEPALIVE, 0); }
>     continue;
> }
> ```
>
> Each HA round pets the mux by writing one byte to `/run/watchdog-mux.sock`
> (`watchdog_update`). With stock 10s rounds, ~9 of every 10 poll iterations time
> out, so the dog is petted ~9x per 10s. Under this patch the LRM wakes on every
> `manager_status` write (so every CRM round) and the two daemons wake each other
> once per second whenever any service is in a transient state — which was
> observed in the journal, alternating `LRM results changed` / `manager status
> changed` lines every second. Socket events then arrive faster than once per
> second, `nfds` is never 0, `WDIOC_KEEPALIVE` is never issued, and the softdog
> (`identity=Software Watchdog`, `timeout=10`) resets the node ~10s later — while
> both daemons are alive and healthy. That is why the CRM master always died
> first and fastest: it is the node with two sub-second writers on one mux.
>
> Evidence: bray's `watchdog-mux` log shows a clean client disconnect at 01:12:03
> and the new patched LRM client added at 01:12:14, then a hard reset 29s later
> with no `watchdog expired` and no `did not stop watchdog` message — so neither
> the 60s client timeout nor an unclean close fired. No core dumps, no OOM, no
> segfaults. ace's LRM/CRM also went silent for 56s before its reset.
>
> ## Prerequisite for any future attempt
>
> Shortening HA rounds is **not** a `pve-ha-manager`-only change: `watchdog-mux`
> must pet `/dev/watchdog` on a time basis (track the last keepalive and issue the
> ioctl whenever ~1s has passed, regardless of socket activity) before any patch
> that makes rounds sub-second is safe. Upstream, that is a separate
> `pve-cluster`/`watchdog-mux` patch that has to land first. This also means the
> round period is load-bearing beyond HA timing, which belongs in the Bugzilla RFE.
>
> The migration-only predecessor (`pve-ha-fast-migrate-patch`) survived months of
> use because its wakes are **one-shot** per migration event, never a sustained
> sub-second cadence — but it shares the hazard class and should be re-read with
> this finding in mind.
>
> Everything below is the original design description, kept as written.

Makes both HA daemons end their round as soon as there is something to do,
instead of always waiting out the fixed 10s round. Every CRM decision — start,
stop, migrate, relocate, recovery, a failed start reported back — is carried out
about a second later instead of 0-10s later per hop.

## Why

`pve-ha-manager` runs the CRM and every LRM on a hard-coded 10s round
(`my $max_time = 10;` + `sleep_until($startime + $max_time)`), with no
`datacenter.cfg` option. A decision crosses up to three hops, each waiting for
its own next round: the LRM reports a result in `nodes/<node>/lrm_status` (0-2s),
the CRM reads it and writes `ha/manager_status` (0-10s), the target LRM reads
that and acts (0-10s). Measured on CT 101: 10s and 12.8s between `vzmigrate`
finishing and `vzstart`.

This is the general form of that fix and the version meant for upstream
(homelab-ops#296): it watches for *any* change rather than only a migration
hand-over.

## What It Changes

The 10s round stays the upper bound; nothing is ever delayed longer than stock.

| Daemon | Wakes when |
|---|---|
| LRM | the pmxcfs version of `ha/manager_status` differs from the one this round's service status was read at, or one of its own workers exited |
| CRM | the pmxcfs version of `ha/crm_commands` changed (a queued `ha-manager` command), or any node's `lrm_status` **results** differ from the ones present right after the last `manage()` |

Three details carry the design:

- **Results, not mtime.** Every LRM rewrites its whole status file each round
  (the `timestamp` field always moves), so a CRM waking on the file changing
  would make the two daemons wake each other once per second forever. Only the
  `{uid => exit_code}` results are compared.
- **The CRM's `crm_commands` snapshot is refreshed.** `manage()` clears that
  file itself through `read_crm_commands()`, so the version cached at round
  start is always stale by the time the round ends — using it would end every
  round early.
- **The LRM's `manager_status` snapshot is not refreshed.** It must stay the
  version the round's own data came from, so a CRM write that landed *while*
  this round worked still wakes it.

## Cost

- One `cfs_update()` IPC call per daemon per second, plus a read of each node's
  `lrm_status` per second in the CRM (that file is not cfs-version-tracked, so
  there is nothing cheaper to compare).
- `flush_master_status()` writes `ha/manager_status` on every `manage()`, so at
  idle each LRM runs roughly one extra round per CRM round (2 per 10s instead of
  1) and writes its status file that much more often. That extra round is also
  exactly what makes a hand-over land in ~1s.

## Not Affected

- **Fencing.** The 60s watchdog and the 120s pmxcfs lock lifetime are
  elapsed-time based; more frequent rounds only renew them sooner.
- **The HA simulator and regression tests.** `sleep_until` in the Sim and Test
  environments ignores the extra callback argument, and `cfs_file_version`
  returns undef outside the PVE2 environment, so `log.expect` output is
  byte-identical.
- Round-*counted* state machines do speed up with more rounds: CRS
  auto-rebalance `hold_duration`, the `group_migration_round` cooldown, and the
  CRM/LRM `$max_active_idle_rounds` idle counters. None of them is reached in
  this cluster (no CRS auto-rebalance, and services are always configured), but
  converting them to elapsed time is an open point before this goes upstream.

## Patched Files

- `/usr/share/perl5/PVE/HA/Env.pm` — `sleep_until` forwards an optional wake
  callback; new `cfs_file_version` (undef unless the plug provides one).
- `/usr/share/perl5/PVE/HA/Env/PVE2.pm` — `sleep_until` checks the callback once
  per second; `cfs_file_version` reads the pmxcfs version, optionally after a
  `cfs_update()`.
- `/usr/share/perl5/PVE/HA/LRM.pm` — arms the wake check for the active state.
- `/usr/share/perl5/PVE/HA/CRM.pm` — arms the wake check for the master state.

All four are checked before any is touched, and validated by loading them as the
daemons do; a failed validation restores every file patched in that run.

## Replaces pve-ha-fast-migrate-patch

The migration-only predecessor (archived under
`archive/retired-modules/top-level/pve-ha-fast-migrate-patch/`) replaced the same
four blocks, so the two cannot be layered. The installer recognises its marker
and refuses with `failed-superseded-*` rather than a generic mismatch. To swap a
node over:

```bash
rm -f /etc/apt/apt.conf.d/99-homelab-pve-ha-fast-migrate-patch \
      /usr/local/sbin/homelab-pve-ha-fast-migrate-patch
rm -rf /var/lib/homelab/pve-ha-fast-migrate-patch \
       /var/lib/homelab/pve-patches/target-checksums/pve-ha-fast-migrate-patch
apt-get install --reinstall -y pve-ha-manager   # restores the stock Perl files
```

Then deploy this module (`./deploy pve-ha-wake-on-change-patch <node>`), which
patches the stock files and restarts both daemons. The pre-patch backups in
`/var/backups/homelab/pve-ha-fast-migrate-patch/` are the offline fallback if the
package cannot be fetched.

## Upstream Tracking

Local until it lands upstream: file the Bugzilla RFE (component HA) with the
measured numbers, then send `[PATCH ha-manager]` to pve-devel. Once an
equivalent is in the package, the original blocks stop matching, the installer
refuses with `failed-unexpected-*`, and this module should be retired.

## Operational Files

- Script: `/usr/local/sbin/homelab-pve-ha-wake-on-change-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-ha-wake-on-change-patch`
- Status: `/var/lib/homelab/pve-ha-wake-on-change-patch/status`
- Backups: `/var/backups/homelab/pve-ha-wake-on-change-patch/`

The apt hook reapplies the patch after package updates and restarts
`pve-ha-lrm.service` plus `pve-ha-crm.service`, under the shared lock
`/run/lock/homelab-pve-patches.lock`. Early wakes log
`ending LRM round early` / `ending CRM round early` to the daemons' journal.
