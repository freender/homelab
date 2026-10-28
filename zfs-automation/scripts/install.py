#!/usr/bin/env python3
"""Remote installer for the zfs-automation module (freender/homelab-ops#30).

Installs sanoid's config and the snapshot and scrub scripts and units, then sets
both managed timers to the state the host's config asks for. Every refusal (env
file, flags, file map, staged files) comes before any write.

Syncoid push replication and the zfs-push receive target were removed in
freender/homelab-ops#271 after every host had been cleaned by hand; no retirement
path is kept.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

SNAPSHOT_TIMER = "homelab-zfs-snapshots.timer"
SCRUB_TIMER = "zfs-scrub.timer"
BASE_ENTRIES = (
    "sanoid.conf",
    "homelab-zfs-snapshots.service",
    SNAPSHOT_TIMER,
    "homelab-zfs-snapshots.sh",
    "homelab-zfs-scrub.sh",
    "zfs-scrub.service",
    SCRUB_TIMER,
)
PACKAGES = ("sanoid",)

REQUIRED_ENV = ("HOMELAB_STATE_DIR", "PAUSED", "ENABLE_ZFS_SNAPSHOTS", "ENABLE_ZFS_SCRUB")
FLAGS = ("PAUSED", "ENABLE_ZFS_SNAPSHOTS", "ENABLE_ZFS_SCRUB")


def preflight(ctx: InstallContext) -> None:
    """Every check that can refuse the deploy."""
    env.require(ctx, *REQUIRED_ENV)
    for name in FLAGS:
        env.flag(ctx, name)

    unmapped = [name for name in BASE_ENTRIES if name not in ctx.file_map]
    if unmapped:
        raise InstallError(f"missing file-map entries: {', '.join(unmapped)}")
    missing = [name for name in ctx.file_map if not (ctx.build_dir / name).is_file()]
    if missing:
        raise InstallError(f"missing staged files: {', '.join(missing)}")


def remove_shadow_copy(ctx: InstallContext) -> None:
    managed = Path(ctx.env["HOMELAB_STATE_DIR"]) / "zfs-automation-managed"
    if managed.is_dir():
        shutil.rmtree(managed)
        log.ok(f"Removed unread shadow copy at {managed}")


def set_timer(ctx: InstallContext, timer: str, enabled: bool) -> None:
    if enabled:
        systemd.ensure_running(ctx, timer, changed=ctx.changes.touched(timer))
    else:
        systemd.ensure_stopped(ctx, timer)


def install(ctx: InstallContext) -> None:
    log.header("ZFS Automation")
    preflight(ctx)

    log.action("TRIM")
    systemd.ensure_running(ctx, "fstrim.timer", changed=False)

    log.action("Sanoid")
    packages.ensure(ctx, *PACKAGES)

    remove_shadow_copy(ctx)

    log.action("Files")
    files.install_all(ctx)

    # The packaged timer would snapshot on sanoid's own schedule, alongside ours.
    systemd.ensure_stopped(ctx, "sanoid.timer")

    units = [name for name in ctx.file_map if name.endswith((".service", ".timer"))]
    if ctx.changes.touched(*units):
        systemd.daemon_reload(ctx)

    if env.flag(ctx, "PAUSED"):
        # A host-wide freeze that overrides every ENABLE_ZFS_* flag.
        systemd.pause(ctx, SNAPSHOT_TIMER, SCRUB_TIMER)
        log.header("ZFS Automation paused")
        return

    set_timer(ctx, SNAPSHOT_TIMER, env.flag(ctx, "ENABLE_ZFS_SNAPSHOTS"))
    set_timer(ctx, SCRUB_TIMER, env.flag(ctx, "ENABLE_ZFS_SCRUB"))


if __name__ == "__main__":
    run(install, "ZFS Automation")
