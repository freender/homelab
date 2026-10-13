"""Staged sudoers validation, shared by `ubuntu-setup` and `guest-baseline`
(freender/homelab-ops#162).

An invalid file in `/etc/sudoers.d` breaks sudo host-wide the instant it lands,
so the staged copy is checked with `visudo -cf` before anything is written.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run


def check_staged(path: Path) -> None:
    if _run(["visudo", "-cf", str(path)], check=False, stdout=subprocess.DEVNULL).returncode:
        raise InstallError("staged sudoers file is invalid; refusing to install")
