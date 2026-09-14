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
    host: str
    script_dir: Path
    build_dir: Path
    env: dict[str, str]
    file_map: dict[str, tuple[str, str]]  # name -> (dest, mode)
    force_update: bool
    changes: ChangeSet = field(default_factory=ChangeSet)
