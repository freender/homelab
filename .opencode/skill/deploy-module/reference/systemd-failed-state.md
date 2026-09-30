# Clearing systemd failed-unit state

Read this when a module's installer manages systemd units and you need to clear,
recover, or retire failed state. Not needed for modules that only render config.

A unit left in `systemctl --failed` after a fix is redeployed stays "failed" until
its next successful run or an explicit `reset-failed` — that gap is what
homelab-alerting/vmalert failed-unit checks see. The `homelab_install.systemd`
helpers (`lib/py/homelab_install/systemd.py`) cover this; reach for them before
running `systemctl reset-failed` by hand. `SKILL.md` carries the
pick-by-what-changed mapping; this file is the semantics of each helper once you
have chosen one.

- **`daemon_reload(ctx)` + `reset_failed(ctx, unit)`, gated on a change** — the
  standard follow-up to `files.install_all`. `reset_failed` clears the unit's
  failed record (ignoring a unit that has none) and never starts it. **The caller
  owns the gate:**

  ```python
  if files.install_all(ctx):
      systemd.daemon_reload(ctx)
      systemd.reset_failed(ctx, "homelab-mymodule.service")
  ```

  The gate is load-bearing: an unconditional reset would hide a real ongoing
  failure until the next redeploy. Note it clears failure state without proving
  the fix works — the unit goes from "known failed" to "unknown" until its next
  run. `pbs-client-backup` is the example (a start there would run a backup).
- **`mask(ctx, unit, reason="")`** — mask a unit that should never run on this
  host (LSB init script with no matching hardware, an unwanted distro default)
  and clear its failed record. Idempotent, and a reported no-op when the unit
  isn't installed; returns True only if it masked something. The reason is
  optional and echoed to output — omit it rather than asserting something
  host-specific you haven't verified. Used by `pve-postinstall`,
  `ubuntu-setup`, and `metrics-exporters`.
- **`recover_failed(ctx, unit, timeout=RECOVER_TIMEOUT_S)`** — for units that
  fail from *transient external* causes (registry rate limits, network blips),
  where a redeploy sees no file change and so the gated reset above does
  nothing. Acts only on a unit currently in the failed state: resets it (which
  also clears the `StartLimitBurst` limiter that otherwise makes systemd refuse
  the start outright) and then starts it, so the unit's own run decides the
  outcome — transient faults recover, persistent ones fail again immediately and
  stay visible. Healthy units are never touched, and a still-failing unit warns
  rather than failing the deploy.

  Only for units that are cheap, idempotent, and safe to run off-schedule.
  `docker` uses it for `homelab-docker-update.service` (a `docker compose up -d`
  oneshot whose `start.sh` pulls images), and `zfs-automation` for unpaused
  replication jobs when `ZFS_REPLICATION_RECOVERY_START_FAILED` is set.
  Deliberately **not** used by `pbs-client-backup` (multi-hour backup) or
  `apt-upgrade` (a start there means running a dist-upgrade at deploy time);
  those have daily timers that clear a stale failure on their next successful
  run, and keeping a possibly-real failure visible beats silencing it. Waits up
  to `RECOVER_TIMEOUT_S` seconds (300), since a `Type=oneshot` start blocks and
  oneshot disables `TimeoutStartSec` by default; the timeout kills the
  `systemctl` client, never the job.
- **`retire_unit(ctx, unit, unit_path)`** — stop, disable, remove, and clear the
  failed record for a unit being retired, with a `daemon-reload` if anything
  changed. Returns **True when it retired something, False when there was
  nothing to do**. Call it once per unit for multi-unit retirements and delete
  any remaining non-unit files (script, textfile-collector output) alongside it
  with `files.remove`; `zfs-automation`'s `retire_obsolete_replication` and the
  `metrics-exporters` `_retire` follow that shape.

One hand-rolled `reset-failed` call site remains on purpose, outside this model:
`docker/scripts/rebuild.sh` (not a module installer).

The helpers are covered in `tests/test_homelab_install.py` against a stubbed
`systemd._run` (the gated `reset_failed` path through
`tests/test_pbs_client_backup_installer.py`) — extend them when changing helper
behavior.
