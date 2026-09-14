"""Systemd enable / verify ladders -- one of the four gaps `utils.sh` never had
(freender/homelab-ops#31 decision 3). Only `ensure_running()` exists; keepalived
is the only caller so far. `daemon-reload` is folded into this call rather than
coalesced across the run (the design doc's proposal) -- there is only one unit
here to coalesce with.
"""

from __future__ import annotations

import subprocess

from . import log
from .context import InstallContext

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
