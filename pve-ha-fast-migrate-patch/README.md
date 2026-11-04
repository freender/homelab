# PVE HA Fast-Migrate Patch

Ends HA manager rounds early while an HA migration or relocation is being handed
over, so a restart-migrated container starts on its target node about a second
after `vzmigrate` finishes instead of waiting for the next fixed rounds.

## Why

`pve-ha-manager` runs both the CRM and every LRM on a hard-coded 10s round
(`my $max_time = 10;` + `sleep_until($startime + $max_time)`), with no
`datacenter.cfg` option. A migration result crosses three hops, each waiting for
its own next round:

1. Source LRM reports the migrate worker's result in `nodes/<node>/lrm_status` (0-2s).
2. CRM reads it, moves the service from `migrate` to `started` on the target,
   writes `ha/manager_status` (0-10s).
3. Target LRM reads that and starts the guest (0-10s).

Measured on CT 101: 10s and 12.8s between vzmigrate finishing and vzstart.

## What It Changes

Only while a migration is in flight; with nothing migrating every round is stock.

| Daemon | Armed when | Wakes when |
|---|---|---|
| Source LRM | it runs a `migrate`/`relocate` worker | that worker exited (result recorded, round restarts and writes `lrm_status`) |
| CRM | a service is in `migrate`/`relocate` and its result is not yet reported | the source node's `lrm_status` carries that service's result uid |
| Target LRM | a service in `migrate`/`relocate` has `target` = this node | `ha/manager_status` changed (pmxcfs version) and that service's uid changed |

The 10s round stays the upper bound. Fencing is untouched: watchdog (60s) and
pmxcfs lock lifetime (120s) are elapsed-time based, early rounds only renew them
sooner. The CRM only arms for results not already present, so a result the
manager ignores (offline node) cannot make it spin.

## Patched Files

- `/usr/share/perl5/PVE/HA/Env.pm` — `sleep_until` forwards an optional wake
  callback; new `manager_status_version` (undef outside the PVE2 env, so the HA
  simulator is unaffected).
- `/usr/share/perl5/PVE/HA/Env/PVE2.pm` — `sleep_until` checks the callback once
  per second; `manager_status_version` reads the pmxcfs version of
  `ha/manager_status`.
- `/usr/share/perl5/PVE/HA/LRM.pm` — arms the source/target wake checks.
- `/usr/share/perl5/PVE/HA/CRM.pm` — arms the result wake check.

All four are checked before any is touched, and validated by loading them as the
daemons do; a failed validation restores every file patched in that run. The
upstream HA simulator suite (`ha-tester.pl`, 153 cases) passes unchanged.

## Why This Stays Migration-Only

The general "wake on any change" version was built and tried on 2026-11-04
(homelab-ops#296) and **self-fenced all three nodes, six hard resets in 16
minutes**. `watchdog-mux` only pets `/dev/watchdog` on an epoll *timeout*
iteration, while each HA round pets the mux by writing to its socket: once rounds
go sub-second, socket events arrive faster than 1/s, the `WDIOC_KEEPALIVE` ioctl
is never reached, and the softdog (10s) resets a node whose daemons are perfectly
healthy. Full post-mortem:
`archive/retired-modules/top-level/pve-ha-wake-on-change-patch/README.md`.

This module survives because its wakes are **one-shot per migration event** — a
worker exiting, a result appearing, a uid changing — so it adds a handful of
early rounds per migration and never a sustained sub-second cadence. That is a
property to preserve, not an accident: never widen a wake condition here to
something that can retrigger every second (a file version that moves every round,
a timestamp, an unconditional poll) without first fixing `watchdog-mux` to pet
the watchdog on a time basis.

## Upstream Tracking

Local, migration-only variant. Anything general enough for upstream needs the
`watchdog-mux` keepalive fix landed first (homelab-ops#296). Once an equivalent
is in the package, the original blocks stop matching, the installer refuses with
`failed-unexpected-*`, and this module should be retired.

## Operational Files

- Script: `/usr/local/sbin/homelab-pve-ha-fast-migrate-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-ha-fast-migrate-patch`
- Status: `/var/lib/homelab/pve-ha-fast-migrate-patch/status`
- Backups: `/var/backups/homelab/pve-ha-fast-migrate-patch/`

The apt hook reapplies the patch after package updates and restarts
`pve-ha-lrm.service` plus `pve-ha-crm.service`, under the shared lock
`/run/lock/homelab-pve-patches.lock`. Early wakes log
`ending LRM round early` / `ending CRM round early` to the daemons' journal.
