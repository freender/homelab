"""File install -- the replacement for `install_build_file` / `install_file_map`
and the `backup_and_*` pair.

Grown demand-driven (freender/homelab-ops#31 decision 3, never a helper with no
caller): `install`/`install_all` with keepalived, `remove` with apt-upgrade,
`install_to`/`ensure_dir`/`backup=` with ssh-config and wsl-conf,
`install_from` with vmalert-rules.

The three install entry points are one primitive with two lookups stacked in
front, narrowest last:

    install_from(src, dest)   arbitrary path  -> explicit destination
    install_to(name, dest)    build/<host>/   -> explicit destination
    install(name)             build/<host>/   -> file-map destination

Built in that direction rather than the reverse because each outer layer assumes
something the host may not support: a destination that only exists on the host
(`~/.ssh/config`) cannot be rendered into a map at build time, and a static
config staged outside `build/` (vmalert's rules) has no per-host directory to be
looked up in either.
"""

from __future__ import annotations

import filecmp
import time
from pathlib import Path

from . import log
from .context import InstallContext
from .errors import InstallError

# Matches `BACKUP_KEEP_COUNT` in lib/utils.sh. Changing it here alone would mean
# a half-ported tree pruned to two different depths depending on which installer
# last touched the file.
BACKUP_KEEP_COUNT = 3


def _backup(dest: Path) -> None:
    """Copy `dest` aside as `<dest>.bak.<timestamp>`, keeping the newest few.

    Same scheme and retention as `backup_config`/`prune_backup_history` in
    lib/utils.sh, deliberately: these siblings are read by a human after a bad
    deploy, and two naming schemes would mean looking in two places.

    Note this is the sibling-file scheme, which AGENTS.md restricts to files that
    are *not* inside an active include directory -- `~/.ssh/config` and
    `/etc/wsl.conf` both qualify. A module writing into somewhere like
    `/etc/apt/apt.conf.d/` must not use it, because the backup would itself be
    parsed as config.
    """
    if not dest.exists():
        return

    backup = dest.with_name(f"{dest.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    backup.write_bytes(dest.read_bytes())

    stale = sorted(dest.parent.glob(f"{dest.name}.bak.*"), reverse=True)[BACKUP_KEEP_COUNT:]
    for path in stale:
        path.unlink()


def ensure_dir(ctx: InstallContext, path: str, mode: str) -> None:
    """Create a directory and pin its mode. `~/.ssh` at 700 is the case that
    needs it -- ssh silently ignores a config in a world-readable directory, so
    creating it with the default umask would be a no-op deploy that looks fine."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(int(mode, 8))


def install_from(
    ctx: InstallContext,
    src: Path,
    dest: str,
    mode: str,
    backup: bool = False,
    record: str | None = None,
) -> bool:
    """Install an arbitrary source path to an explicit destination.

    The deepest primitive: `install_to` is this with the source pinned to
    `ctx.build_dir`, and `install` is the file-map lookup in front of that.

    Added for `vmalert-rules` (freender/homelab-ops#30), the first module whose
    sources are not rendered per-host at all. Its rules are static `configs/`
    staged to `<remote_root>/rules/`, so there is no `build/<host>/` for them to
    live in and nothing to look them up by.

    `record` is the key written to `ctx.changes`, defaulting to `dest`.
    `install_to` passes the file-map *name* instead, because that is what callers
    like `apt-upgrade` query with (`ctx.changes.touched("service", "timer")`) --
    recording the destination path there would silently break those checks.
    """
    if not src.is_file():
        raise InstallError(f"missing source file: {src}")

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
    if backup:
        _backup(dest_path)
    dest_path.write_bytes(src.read_bytes())
    dest_path.chmod(mode_bits)
    log.sub(f"Updated {dest}")
    ctx.changes.record(record or dest)
    return True


def install_to(
    ctx: InstallContext, name: str, dest: str, mode: str, backup: bool = False
) -> bool:
    """Install a build file to an explicit destination, bypassing the file map.

    For destinations that are only knowable on the host. `ssh-config` writes
    `~/.ssh/config`, and the orchestrator cannot render that into a map: it
    knows `config.user` but not whether that user's home is `/home/<user>` or
    `/root`, and guessing wrong writes a config ssh will never read.
    """
    return install_from(ctx, ctx.build_dir / name, dest, mode, backup=backup, record=name)


def install(ctx: InstallContext, name: str, backup: bool = False) -> bool:
    """Install one file-map entry from `ctx.build_dir`. Returns True if the
    destination changed (new, or content/force differed from what was there).

    `backup=True` keeps a timestamped copy of what was there first, for the
    files where being wrong locks you out of fixing it remotely.
    """
    try:
        dest, mode = ctx.file_map[name]
    except KeyError as exc:
        raise InstallError(f"missing file-map entry: {name}") from exc
    return install_to(ctx, name, dest, mode, backup=backup)


def remove(ctx: InstallContext, dest: str, reason: str = "") -> bool:
    """Delete a managed file that should no longer be on the host.

    Takes an absolute destination rather than a file-map name, because the case
    that needs it is a file the map no longer contains: `apt-upgrade` stops
    rendering `auto-reboot.conf` the moment `auto_reboot` goes false, so at the
    point the drop-in has to come off the host there is no map entry left to
    look it up by. Removing it is what makes the flag reversible -- without this
    a host keeps rebooting itself after the flag was taken away.

    Returns True if a file was actually removed.
    """
    path = Path(dest)
    if not path.exists():
        return False

    path.unlink()
    log.sub(f"Removed {dest}{f' ({reason})' if reason else ''}")
    ctx.changes.record(dest)
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
