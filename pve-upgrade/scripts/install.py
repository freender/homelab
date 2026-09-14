#!/usr/bin/env python3
"""Remote installer for the pve-upgrade module (freender/homelab-ops#30).

Unlike every module ported so far, this one installs nothing: its deploy action
*is* an `apt-get dist-upgrade` on the target, which is why `--confirm-upgrade`
gates it and why `include_in_all=False` keeps it out of `deploy all`. So there is
no file map, no unit, and nothing to converge -- the whole installer is a guard,
a library call, and a report.

`PAUSED` arrives on the process environment, not in a `build/<host>/env` file:
`simple_root_installer_deploy` stages only `scripts/`, so there is no build
directory to render one into. `env.deploy_flag` is the strict read of that
channel -- see `InstallContext` for why the two channels stay distinct.

**The reboot report is a warning and never an action.** These are PVE cluster
nodes, PBS and PDM; rebooting one as a side effect of a deploy could take down
quorum or a live backup. The reboot decision is a human one, made against a named
plan (AGENTS.md), and this only supplies the input to it.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

from homelab_install import env, log, packages, run
from homelab_install.context import InstallContext

REBOOT_REQUIRED = Path("/var/run/reboot-required")
BOOT_DIR = Path("/boot")
KERNEL_GLOB = "vmlinuz-*"
KERNEL_PREFIX = "vmlinuz-"


def _version_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key approximating `sort -V`, which is what the bash used.

    Splits into digit and non-digit runs and compares digits numerically, so
    `6.14.11-4` sorts above `6.14.9-1` where a plain string compare would not --
    the case that actually matters here, since a host one point release behind
    would otherwise be reported as already running the newest kernel.

    Each element carries its own type tag so a digit run is never compared
    against a text run; tuples of mixed `int`/`str` raise `TypeError` in Python,
    which would turn a cosmetic ordering question into a crashed deploy.
    """
    parts: list[tuple[int, int, str]] = []
    for chunk in re.split(r"(\d+)", version):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk), ""))
        else:
            parts.append((1, 0, chunk))
    return tuple(parts)


def newest_installed_kernel() -> str | None:
    """The highest-versioned kernel image in /boot, or None if there are none.

    None is the normal, healthy answer on an LXC container: it boots the host's
    kernel and has no `/boot/vmlinuz-*` of its own, so there is no such thing as
    a pending kernel for it. The bash skipped those hosts via `compgen -G` for
    the same reason.
    """
    names = [
        path.name[len(KERNEL_PREFIX) :]
        for path in BOOT_DIR.glob(KERNEL_GLOB)
        if path.name.startswith(KERNEL_PREFIX)
    ]
    return max(names, key=_version_key) if names else None


def reboot_reason(running_kernel: str) -> str | None:
    """Why this host needs a reboot, or None.

    Two independent signals, because the first is unreliable on exactly the hosts
    this module targets: PVE and PBS do not ship `update-notifier-common`, so
    `/var/run/reboot-required` is never written there even after a kernel upgrade.
    Comparing the running kernel against the newest installed one is the fallback
    that actually fires on those nodes.
    """
    if REBOOT_REQUIRED.is_file():
        return "reboot-required flag"

    newest = newest_installed_kernel()
    if newest is not None and newest != running_kernel:
        return f"running {running_kernel}, newest installed {newest}"
    return None


def install(ctx: InstallContext) -> None:
    log.header("PVE/PBS/PDM Upgrade")

    # `socket.gethostname()` rather than `ctx.host`: the inventory name and the
    # host's own name differ for some hosts, and this string is read next to the
    # host's own logs. Same reasoning as apt-upgrade's report_reboot_required.
    hostname = socket.gethostname()

    if env.deploy_flag(ctx, "PAUSED"):
        log.sub(f"Paused via pve-upgrade.paused; skipping apt dist-upgrade on {hostname}")
        return

    packages.dist_upgrade(ctx)
    log.ok(f"Upgrade complete on {hostname}")

    reason = reboot_reason(os.uname().release)
    if reason:
        log.warn(f"Reboot required on {hostname} ({reason})")


if __name__ == "__main__":
    run(install, "PVE/PBS/PDM Upgrade")
