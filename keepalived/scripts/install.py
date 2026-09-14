#!/usr/bin/env python3
"""Remote installer for the keepalived module — the first port to `homelab_install`.

Replaces the 66-line `install.sh` that was here (freender/homelab-ops#30/#36). The
whole body is the four statements below; everything the bash version spent lines on
— the 19-line preamble, `require_dir`/`require_file`, the two `rc=` dances around
`install_build_file`, the conditional `daemon-reload`, and the enable /
restart-if-changed / start-if-dead ladder — is in the library.

`homelab_install` is imported off `PYTHONPATH`, which `stage_and_run_remote_installer`
sets to the staged `{remote_root}/lib/py` because this file's name ends in `.py`. It
is never run outside that staging, so there is no `sys.path` fallback here.

Note the ordering: `files.install_all(ctx)` must run before `ensure_running`, and
`changed=` must come from `ctx.changes` rather than a local flag. A changed
`keepalived.conf` with no restart is a live config and a stale process — on the host
that owns VIP 10.0.40.15.
"""

from __future__ import annotations

from homelab_install import files, log, packages, run, systemd
from homelab_install.context import InstallContext


def install(ctx: InstallContext) -> None:
    log.header("Keepalived")
    log.action("Package")
    packages.ensure(ctx, "keepalived", "curl", probe="keepalived")
    files.install_all(ctx)
    systemd.ensure_running(ctx, "keepalived", changed=ctx.changes.any())


if __name__ == "__main__":
    run(install, "Keepalived")
