"""Systemd enable / verify ladders -- one of the four gaps `utils.sh` never had
(freender/homelab-ops#31 decision 3). `daemon-reload` is folded into these calls
rather than coalesced across the run (the design doc's proposal) -- there are at
most two units in play per module so far.

`pause()`, `retire_unit()` and `run_once()` arrived with `apt-upgrade`
(freender/homelab-ops#30), which is the first module to need any of them;
`ensure_stopped()` and `recover_failed()` with `docker`; `mask()` with
`ubuntu-setup`; `reset_failed()` with `pbs-client-backup`; `require_active()` with
`metrics-exporters`.

**`pause()` deliberately does not reproduce `homelab_apply_pause`'s return
convention.** That helper returns 0 when paused and 1 when not, so every caller
reads `if homelab_apply_pause ...; then` as "if paused" -- inverted against
ordinary shell truthiness, which is why the skill has to warn about it. Here the
caller owns the branch (`if paused: systemd.pause(...); return`) and this
function only does the stopping, so there is no inverted code to misread.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import log
from .context import InstallContext
from .errors import InstallError

# Indirection point for tests: patch `homelab_install.systemd._run`. See
# packages.py for the same pattern and rationale.
_run = subprocess.run


def _is_enabled(unit: str) -> bool:
    return _run(["systemctl", "is-enabled", "--quiet", unit], check=False).returncode == 0


def _is_active(unit: str) -> bool:
    return _run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0


def ensure_running(ctx: InstallContext, unit: str, changed: bool) -> None:
    """Enable / restart-if-changed / start-if-dead ladder, replacing
    `install.sh`'s hand-rolled enable/restart/start block."""
    if changed:
        _run(["systemctl", "daemon-reload"], check=True)

    if not _is_enabled(unit):
        _run(["systemctl", "enable", "--now", unit], check=True)
        log.ok(f"{unit} enabled")
    elif changed:
        _run(["systemctl", "restart", unit], check=True)
        log.ok(f"{unit} restarted")
    elif not _is_active(unit):
        _run(["systemctl", "start", unit], check=True)
        log.ok(f"{unit} started")
    else:
        log.sub(f"{unit} already enabled")


def enable(ctx: InstallContext, unit: str) -> None:
    """Enable a unit for the next boot without starting it now.

    Arrives with `apcupsd`, whose `homelab-ha-rearm.service` is a boot-time
    oneshot that runs `ha-manager crm-command arm-ha`. `ensure_running` would
    `enable --now` it, and starting it on a deploy would re-arm HA on a cluster
    the operator may have disarmed on purpose. This is for units whose *start*
    is the action, not the service.
    """
    if _is_enabled(unit):
        log.sub(f"{unit} already enabled")
        return
    _run(["systemctl", "enable", unit], check=True)
    log.ok(f"{unit} enabled")


def require_active(ctx: InstallContext, *units: str) -> None:
    """Fail the deploy unless every unit is active right now.

    The port of the `systemctl is-active --quiet` lines `metrics-exporters` ends
    with, which under `set -e` failed the deploy on the first inactive unit. All
    units are checked and named together, so one run reports every dead exporter
    rather than the first.

    A point-in-time check: a service that crashes a second after starting still
    passes. It catches a unit that never came up, which is what it is for.
    """
    inactive = [unit for unit in units if not _is_active(unit)]
    if inactive:
        raise InstallError(f"not active after deploy: {', '.join(inactive)}")


def daemon_reload(ctx: InstallContext) -> None:
    """Make systemd re-read unit files after one has been rewritten.

    Needed on its own by any module that writes a unit and then acts on it
    *without* going through `ensure_running` -- `apt-upgrade` rewrites the
    service and then either pauses, or starts it directly, and starting a
    rewritten unit without this runs the definition systemd still has cached.
    """
    _run(["systemctl", "daemon-reload"], check=True)


def reset_failed(ctx: InstallContext, unit: str) -> None:
    """Clear a unit's failed record, ignoring a unit that has none.

    The second half of `homelab_reload_and_clear_failed`, arriving with
    `pbs-client-backup`: after a changed definition is installed, the old failure
    belongs to the old definition. Deliberately never starts the unit -- that is
    `recover_failed`, and for a backup job it would start a backup from a deploy.
    """
    _run(["systemctl", "reset-failed", unit], check=False, capture_output=True)


def ensure_stopped(ctx: InstallContext, unit: str) -> bool:
    """Stop and disable one unit, leaving its unit file installed. Returns True
    if it had to act.

    The `ensure_running` counterpart, and the replacement for
    `ensure_timer_state`'s disabled branch. Arrived with `docker`, where a host
    without `docker.update_schedule` (ghost) must not keep an update timer that an
    earlier config enabled. `pause` is this per unit plus its own heading.

    Units already stopped are reported rather than skipped silently, because
    "already stopped" and "I stopped it" are different facts when reading a
    deploy log after an incident.
    """
    if _is_active(unit) or _is_enabled(unit):
        _run(["systemctl", "disable", "--now", unit], check=True)
        log.ok(f"{unit} stopped and disabled")
        return True
    log.sub(f"{unit} already stopped")
    return False


def pause(ctx: InstallContext, *units: str) -> None:
    """Stop and disable each unit, leaving its unit file installed.

    Pause is reversible, so the files stay: removing them is retirement, and a
    resume would then have nothing to re-enable.
    """
    log.action("Pausing")
    for unit in units:
        if unit:
            ensure_stopped(ctx, unit)


def retire_unit(ctx: InstallContext, unit: str, unit_path: str) -> bool:
    """Disable, clear failed state, and delete a unit that should no longer exist.

    Returns True if anything was actually removed. `reset-failed` is
    unconditional and ignores its own failure: a unit that was never loaded has
    no failed state to clear, and that is the normal case here rather than an
    error.
    """
    changed = False
    if _is_enabled(unit) or _is_active(unit):
        _run(["systemctl", "disable", "--now", unit], check=True)
        changed = True
        log.sub(f"Retired {unit}")

    _run(["systemctl", "reset-failed", unit], check=False, capture_output=True)

    path = Path(unit_path)
    if path.exists():
        path.unlink()
        changed = True
        log.sub(f"Removed {unit_path}")

    if changed:
        _run(["systemctl", "daemon-reload"], check=True)
    return changed


def run_once(ctx: InstallContext, unit: str) -> None:
    """Start a oneshot unit and wait for it, surfacing a failure as InstallError.

    `systemctl start` on a `Type=oneshot` unit blocks until it finishes, so a
    non-zero exit here means the job itself failed -- which the bash discarded,
    because `set -e` was disabled around nothing and `systemctl start` was called
    bare. A failed dist-upgrade now fails the deploy.
    """
    result = _run(["systemctl", "start", unit], check=False)
    if result.returncode != 0:
        raise InstallError(f"{unit} failed (systemctl start exited {result.returncode})")


# Inherited from `HOMELAB_RECOVER_TIMEOUT`'s default in the retired lib/utils.sh
# (homelab-ops#38); this is now its only definition. Long enough for the slowest
# managed unit to start, short enough that a wedged one does not hold a deploy.
RECOVER_TIMEOUT_S = 300


def _is_failed(unit: str) -> bool:
    return _run(["systemctl", "is-failed", "--quiet", unit], check=False).returncode == 0


def recover_failed(ctx: InstallContext, unit: str, timeout: int = RECOVER_TIMEOUT_S) -> None:
    """Reset and restart a unit only if it is currently failed. Never fails the deploy.

    The port of `homelab_recover_failed_units`, for units that fail from transient
    external causes -- `homelab-docker-update.service` pulls images, and GHCR rate
    limiting fails it with no file change for a redeploy to notice. `reset-failed`
    also clears the `StartLimitBurst` limiter, without which systemd refuses the
    start outright until the next timer fire.

    A still-failing unit warns rather than raising: the unit's own run decides
    the outcome, and leaving it failed keeps it visible to alerting.

    Three outcomes where the bash reported two. It could not tell `timeout(1)`'s
    124 from a start that failed, so any non-zero start that left the unit
    not-failed read as "did not settle within 300s" -- but the docker unit is
    `Restart=on-failure`, and a unit parked in `activating (auto-restart)` is also
    not failed. Only a real timeout is reported as one here. The timeout kills the
    `systemctl` client, never the job, same as `timeout(1)` did.
    """
    if not _is_failed(unit):
        return

    log.action(f"Recovering failed {unit}")
    _run(["systemctl", "reset-failed", unit], check=False, capture_output=True)
    try:
        result = _run(["systemctl", "start", unit], check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warn(f"{unit} did not settle within {timeout}s; job still running")
        return

    if result.returncode == 0:
        log.ok(f"{unit} recovered")
    elif _is_failed(unit):
        log.warn(f"{unit} still failing after restart; left failed for alerting")
    else:
        log.warn(f"{unit} failed again and is waiting on its restart policy")


def mask(ctx: InstallContext, unit: str, reason: str = "") -> bool:
    """Mask a unit that must never run on this host, and clear its failed record.

    The port of `homelab_mask_unwanted_service`. A unit that is not installed is
    a reported no-op, since the distro default this exists for (`openipmi`, an LSB
    script that fails at boot with no BMC) is absent on some hosts. Returns True
    if it masked anything.

    The stop before masking ignores its own failure, as the bash did: the unit
    being masked is by definition one that does not work here, and `mask` is the
    step whose failure matters.
    """
    if _run(["systemctl", "list-unit-files", unit], check=False, capture_output=True).returncode:
        log.sub(f"{unit} not installed; nothing to mask")
        return False

    state = _run(["systemctl", "is-enabled", unit], check=False, capture_output=True, text=True)
    if (state.stdout or "").strip() == "masked":
        _run(["systemctl", "reset-failed", unit], check=False, capture_output=True)
        log.sub(f"{unit} already masked")
        return False

    _run(["systemctl", "disable", "--now", unit], check=False, capture_output=True)
    _run(["systemctl", "mask", unit], check=True)
    _run(["systemctl", "reset-failed", unit], check=False, capture_output=True)
    log.ok(f"{unit} masked{f' ({reason})' if reason else ''}")
    return True
