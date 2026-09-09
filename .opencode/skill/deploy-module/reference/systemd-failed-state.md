# Clearing systemd failed-unit state

Read this when a module's installer manages systemd units and you need to clear,
recover, or retire failed state. Not needed for modules that only render config.

A unit left in `systemctl --failed` after a fix is redeployed stays "failed" until
its next successful run or an explicit `reset-failed` — that gap is what
homelab-alerting/vmalert failed-unit checks see. Four shared `lib/utils.sh`
helpers cover this; reach for them before writing `systemctl reset-failed` by hand.
Which one you want depends on whether the redeploy changed anything:
changed content -> `homelab_reload_and_clear_failed`; unchanged content but a
transient fault -> `homelab_recover_failed_units`; unit going away ->
`retire_systemd_unit`.

- **`homelab_reload_and_clear_failed "$changed" unit1 [unit2 ...]`** — the
  standard follow-up to `install_file_map`. Runs `daemon-reload` and clears the
  named units' failed records, but only when the caller's changed flag is
  `true`. **The helper owns the gate — call it unguarded**, not inside another
  `if [[ "$changed" == true ]]`:

  ```bash
  changed=false
  install_file_map || rc=$?
  [[ $rc -eq 0 ]] && changed=true

  homelab_reload_and_clear_failed "$changed" homelab-mymodule.service
  ```

  The gate is load-bearing: an unconditional reset would hide a real ongoing
  failure until the next redeploy. Note it clears failure state without proving
  the fix works — the unit goes from "known failed" to "unknown" until its next
  run. Where an immediate verdict matters, follow it with an explicit
  `systemctl start` and check the result, as `zfs-automation`'s replication
  recovery does.
- **`homelab_mask_unwanted_service unit.service ["reason"]`** — mask a unit that
  should never run on this host (LSB init script with no matching hardware, an
  unwanted distro default) and clear its failed record. Idempotent, and a
  reported no-op when the unit isn't installed. The reason is optional and
  echoed to output — omit it rather than asserting something host-specific you
  haven't verified. Used by `pve-postinstall` and `ubuntu-setup`.
- **`homelab_recover_failed_units unit1 [unit2 ...]`** — for units that fail
  from *transient external* causes (registry rate limits, network blips), where
  a redeploy sees no file change and so the gated helper above does nothing.
  Acts only on units currently in the failed state: resets them (which also
  clears the `StartLimitBurst` limiter that otherwise makes systemd refuse the
  start outright) and then starts them, so the unit's own run decides the
  outcome — transient faults recover, persistent ones fail again immediately
  and stay visible. Healthy units are never touched, and a still-failing unit
  warns rather than failing the deploy.

  Only for units that are cheap, idempotent, and safe to run off-schedule.
  `docker` uses it for `homelab-docker-update.service` (a `docker compose up -d`
  oneshot whose `start.sh` pulls images). Deliberately **not** used by
  `pbs-client-backup` (multi-hour backup) or `apt-upgrade` (a start there means
  running a dist-upgrade at deploy time); those have daily timers that clear a
  stale failure on their next successful run, and keeping a possibly-real
  failure visible beats silencing it. Waits up to `HOMELAB_RECOVER_TIMEOUT`
  seconds (default 300), since a `Type=oneshot` start blocks and oneshot
  disables `TimeoutStartSec` by default.
- **`retire_systemd_unit unit-name /path/to/unit-file`** — stop, disable,
  remove, and clear the failed record for a unit being retired. Returns **0
  when it retired something, 1 when there was nothing to do** (the
  `copy_if_changed` convention). Under `set -e` a bare call therefore aborts
  the installer on the common no-op path — consume the status with `if ...;
  then`, a flag assignment, or an explicit `|| true`. Call it once per unit for
  multi-unit retirements and delete any remaining non-unit files (script,
  textfile-collector output) alongside it; `zfs-automation`'s
  `cleanup_retired_health_check` and both `metrics-exporters` cleanups follow
  that shape.

Two hand-rolled `reset-failed` call sites remain on purpose, both outside this
model: `zfs-automation`'s replication recovery (resets *and* starts, to get a
verdict) and `docker/scripts/rebuild.sh` (not a module installer).

The systemd helpers are covered in `tests/test_safety_regressions.py` and the
file helpers in `tests/test_utils_file_helpers.py`, both running real bash
against a stubbed `systemctl` — extend them when changing helper behavior.
