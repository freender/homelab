#!/usr/bin/env python3
"""THROWAWAY PROTOTYPE -- freender/homelab-ops#33. Variant A: free functions,
`ctx` first. Not wired into `./deploy`, not run against helm/neo/tower. See
`install_facade.py` for variant B and the issue for the verdict; this file (or
its successor) replaces `install.sh` for real only in freender/homelab-ops#36.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prototype-only affordance: freender/homelab-ops#34 (not done yet) is what
# teaches the deploy path to put `lib/py` on PYTHONPATH for a real port.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib" / "py"))

from homelab_install import files, log, packages, run, systemd  # noqa: E402
from homelab_install.context import InstallContext  # noqa: E402


def install(ctx: InstallContext) -> None:
    log.header("Keepalived")
    packages.ensure(ctx, "keepalived", "curl", probe="keepalived")
    files.install_all(ctx)
    systemd.ensure_running(ctx, "keepalived", changed=ctx.changes.any())


if __name__ == "__main__":
    run(install, "Keepalived")
