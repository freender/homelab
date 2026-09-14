"""File-map install, the replacement for `install_build_file` / `install_file_map`.

Only what keepalived needs is here (freender/homelab-ops#31 decision 3: the
library's gaps land demand-driven, never a helper with no caller). `has()` and
`remove()` arrive with the modules that actually call them.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

from . import log
from .context import InstallContext
from .errors import InstallError


def install(ctx: InstallContext, name: str) -> bool:
    """Install one file-map entry from `ctx.build_dir`. Returns True if the
    destination changed (new, or content/force differed from what was there)."""
    try:
        dest, mode = ctx.file_map[name]
    except KeyError as exc:
        raise InstallError(f"missing file-map entry: {name}") from exc

    src = ctx.build_dir / name
    if not src.is_file():
        raise InstallError(f"missing build file: {src}")

    dest_path = Path(dest)
    mode_bits = int(mode, 8)
    unchanged = (
        not ctx.force_update
        and dest_path.is_file()
        and filecmp.cmp(src, dest_path, shallow=False)
    )

    if unchanged:
        dest_path.chmod(mode_bits)
        log.sub(f"{dest} unchanged; skipping update")
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(src.read_bytes())
    dest_path.chmod(mode_bits)
    log.sub(f"Updated {dest}")
    ctx.changes.record(name)
    return True


def install_all(ctx: InstallContext, exclude: tuple[str, ...] = ()) -> bool:
    """Install every file-map entry not in `exclude`. Returns True if anything
    changed. Replaces `install_file_map` and the per-call `rc=` dance."""
    changed = False
    for name in ctx.file_map:
        if name in exclude:
            continue
        if install(ctx, name):
            changed = True
    return changed
