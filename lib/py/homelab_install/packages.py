"""Apt ensure-installed -- one of the four gaps `utils.sh` never had
(freender/homelab-ops#31 decision 3). Only `ensure()` exists; the "one lazy
`apt-get update` per run" the design doc proposes has no caller yet (keepalived
never called `apt-get update` either) and is deferred until a module needs it.
"""

from __future__ import annotations

import shutil
import subprocess

from . import log
from .context import InstallContext
from .errors import InstallError

# Indirection point for tests: patch `homelab_install.packages._run` instead of
# shelling out to a real apt-get. Keeps this in-process testable per
# freender/homelab-ops#31 decision 4, no subprocess-against-a-sandbox needed.
_run = subprocess.run


def ensure(ctx: InstallContext, *packages: str, probe: str | None = None) -> None:
    """Install `packages` with apt unless `probe` is already on PATH."""
    if probe is not None and shutil.which(probe) is not None:
        log.sub(f"{probe} already installed")
        return

    result = _run(["apt-get", "install", "-y", "-q", *packages], check=False)
    if result.returncode != 0:
        raise InstallError(f"failed to install packages: {', '.join(packages)}")
    log.ok(f"{' '.join(packages)} installed")
