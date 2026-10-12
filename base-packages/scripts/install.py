#!/usr/bin/env python3
"""Remote installer for the base-packages module — the second port to
`homelab_install` (freender/homelab-ops#30, order set by #35(d)).

Replaces the 65-line `install.sh` that was here. It is the smallest caller of
`simple_root_installer_deploy`, and that is why it went second: #34 taught that
helper `installer=`/`interpreter=`, but keepalived bypasses it entirely, so until
this module deploys, the helper's Python path has only ever run in unit tests.
The worst case of a bad deploy here is a package check that refuses to run — no
file map, no systemd units, nothing to bounce.

`BASE_PACKAGES` comes from `ctx.deploy_env`, not `ctx.env`. This module has no
build directory — `simple_root_installer_deploy` stages only `scripts/` — so the
orchestrator's `env=` on the remote command line is the only channel it has.

Everything the bash spent lines on — the 19-line preamble, splitting the list,
the two `dpkg -s` loops, and the re-verify-after-install that stops a package
which resolves but fails to configure from being reported installed — is in
`packages.ensure()`.

`BACKPORTS_KERNEL` moves the kernel metapackage to `<codename>-backports` once.
`packages.ensure` cannot do it, because the stable kernel is already installed.
After that the scheduled dist-upgrade keeps it current: backports is
`ButAutomaticUpgrades`, so a package installed from it follows newer backports
uploads. Turning the flag off again does not downgrade anything.
"""

from __future__ import annotations

import os
import subprocess

from homelab_install import env, log, osinfo, packages, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.packages._run`.
_run = subprocess.run

OS_RELEASE = "/etc/os-release"
KERNEL_PACKAGE = "linux-image-amd64"
BACKPORTS_MARKER = "~bpo"


def installed_version(package: str) -> str:
    result = _run(
        ["dpkg-query", "-W", "-f=${Version}", package],
        check=False,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def backports_suite() -> str:
    values = osinfo.os_release(OS_RELEASE)
    codename = values.get("VERSION_CODENAME", "")
    if values.get("ID") != "debian" or not codename:
        raise InstallError(f"BACKPORTS_KERNEL needs a Debian host; {OS_RELEASE} says otherwise")
    return f"{codename}-backports"


def _apt(*args: str) -> None:
    result = _run(
        [*packages.APT_GET, *args],
        check=False,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if result.returncode != 0:
        raise InstallError(f"apt-get {args[0]} failed (exit {result.returncode})")


def ensure_backports_kernel() -> None:
    suite = backports_suite()
    current = installed_version(KERNEL_PACKAGE)
    if BACKPORTS_MARKER in current:
        log.sub(f"{KERNEL_PACKAGE} {current} already tracks {suite}")
        return

    log.action(f"Moving {KERNEL_PACKAGE} {current or '(not installed)'} to {suite}")
    _apt("update", "-qq")
    _apt("install", "-y", "-q", "-t", suite, KERNEL_PACKAGE)

    # Same reasoning as packages.ensure's re-verify: apt can exit 0 and still
    # leave the old version, e.g. when the suite is missing from the sources.
    installed = installed_version(KERNEL_PACKAGE)
    if BACKPORTS_MARKER not in installed:
        raise InstallError(f"{KERNEL_PACKAGE} is still {installed or 'absent'} after install")
    log.ok(f"Installed {KERNEL_PACKAGE} {installed}")
    log.warn("The new kernel runs only after a reboot; this module never reboots")


def install(ctx: InstallContext) -> None:
    log.header("Base Packages")

    requested = ctx.deploy_env.get("BASE_PACKAGES", "").split()
    if not requested:
        # Refuse rather than no-op. An empty list here means the orchestrator
        # failed to render one, and silently succeeding would report every host
        # as having a baseline it does not have — the exact drift this module
        # exists to stop.
        raise InstallError("BASE_PACKAGES is empty; refusing to run")

    packages.ensure(ctx, *requested)

    if env.deploy_flag(ctx, "BACKPORTS_KERNEL"):
        ensure_backports_kernel()


if __name__ == "__main__":
    run(install, "Base Packages")
