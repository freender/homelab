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
"""

from __future__ import annotations

from homelab_install import log, packages, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError


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


if __name__ == "__main__":
    run(install, "Base Packages")
