"""State passed to every `install(ctx)` module entry point.

Free-function core (freender/homelab-ops#31 decision 2, amended): no facades
live here. `InstallContext` is a plain state dataclass -- frozen except for the
`ChangeSet` it deliberately mutates -- and the library's logic lives in free
functions in `files.py` / `packages.py` / `systemd.py` that take `ctx` first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .changes import ChangeSet


@dataclass(frozen=True)
class InstallContext:
    """The two env dicts are different channels and must not be conflated.

    `env` is the parsed `build/<host>/env` file -- config the orchestrator
    *rendered*. `deploy_env` is the process environment, which is how
    `run_remote_installer(env=...)` delivers values for a module that has no
    build directory to render into: `simple_root_installer_deploy` stages only
    `scripts/`, so the command line is its only channel. `base-packages` reads
    `BASE_PACKAGES` from it, and `pve-upgrade` is the other caller of that shape.

    In bash both landed in the same shell namespace and `require_env` could not
    tell them apart, which is exactly how a module ends up reading a value the
    orchestrator never sent and finding whatever the calling shell had.
    """

    host: str
    script_dir: Path
    build_dir: Path
    env: dict[str, str]
    deploy_env: dict[str, str]
    file_map: dict[str, tuple[str, str]]  # name -> (dest, mode)
    force_update: bool
    changes: ChangeSet = field(default_factory=ChangeSet)
