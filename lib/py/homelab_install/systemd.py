"""Systemd enable / verify ladders -- one of the four gaps `utils.sh` never had
(freender/homelab-ops#31 decision 3). `daemon-reload` is folded into these calls
rather than coalesced across the run (the design doc's proposal) -- there are at
most two units in play per module so far.

`pause()`, `retire_unit()` and `run_once()` arrived with `apt-upgrade`
(freender/homelab-ops#30), which is the first module to need any of them.

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


def daemon_reload(ctx: InstallContext) -> None:
    """Make systemd re-read unit files after one has been rewritten.

    Needed on its own by any module that writes a unit and then acts on it
    *without* going through `ensure_running` -- `apt-upgrade` rewrites the
    service and then either pauses, or starts it directly, and starting a
    rewritten unit without this runs the definition systemd still has cached.
    """
    _run(["systemctl", "daemon-reload"], check=True)


def pause(ctx: InstallContext, *units: str) -> None:
    """Stop and disable each unit, leaving its unit file installed.

    Pause is reversible, so the files stay: removing them is retirement, and a
    resume would then have nothing to re-enable. Units already stopped are
    reported rather than skipped silently, because "already stopped" and "I
    stopped it" are different facts when reading a deploy log after an incident.
    """
    log.action("Pausing")
    for unit in units:
        if not unit:
            continue
        if _is_active(unit) or _is_enabled(unit):
            _run(["systemctl", "disable", "--now", unit], check=True)
            log.ok(f"{unit} stopped and disabled")
        else:
            log.sub(f"{unit} already stopped")


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
