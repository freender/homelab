"""Shared, stdlib-only remote installer library.

Prototype scope (freender/homelab-ops#33): only what `keepalived/scripts/install.py`
needs -- the `run()` harness, `ChangeSet`, `files.install_all()`,
`packages.ensure()`, `systemd.ensure_running()`. See
`freender/homelab-ops#30`/`#31` for the map and the settled API decisions this
implements, and the companion Obsidian doc "Homelab Repository - Python
Installers" for the full design.

Hermetic against `src/homelab/` in one direction only (decision 4): this
package must never import from the orchestrator. `src/homelab/` and `tests/`
may import this package.
"""

from __future__ import annotations

from . import env, files, log, packages, systemd
from .changes import ChangeSet
from .context import InstallContext
from .errors import InstallError
from .main import run

__all__ = [
    "ChangeSet",
    "InstallContext",
    "InstallError",
    "env",
    "files",
    "log",
    "packages",
    "run",
    "systemd",
]
