#!/usr/bin/env python3
"""Remote installer for the wsl-conf module (freender/homelab-ops#30).

The safety guard is the point of this module, not the copy. A `hosts.conf`
mistake -- `type: ubuntu` reused on a real VM or LXC -- would otherwise write
`/etc/wsl.conf` onto a host where it means nothing, and the file is inert enough
there that nobody would notice until something else went looking for it. So the
installer refuses unless the kernel actually looks like WSL, checked two ways
because neither alone is reliable across WSL versions.

The reboot notice is deliberately a warning rather than an action: `wsl
--shutdown` has to be run from Windows PowerShell, and running it from inside
this session would kill the session mid-deploy.
"""

from __future__ import annotations

from pathlib import Path

from homelab_install import files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

WSL_CONF_PATH = "/etc/wsl.conf"
WSL_CONF_MODE = "644"

BINFMT_MARKER = Path("/proc/sys/fs/binfmt_misc/WSLInterop")
VERSION_PATH = Path("/proc/version")


def looks_like_wsl() -> bool:
    """Two independent signals, because neither is reliable on its own.

    `WSLInterop` is absent when interop is disabled in `wsl.conf` -- which this
    very module can be what disables -- and the `microsoft` kernel signature is
    absent on custom-built kernels. Requiring both would make the module refuse
    to run on hosts it is meant for.
    """
    if BINFMT_MARKER.exists():
        return True
    try:
        return "microsoft" in VERSION_PATH.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def install(ctx: InstallContext) -> None:
    log.header("WSL Conf")

    if not looks_like_wsl():
        raise InstallError(
            "This does not look like a WSL host (no WSLInterop, no microsoft "
            f"kernel signature); refusing to install {WSL_CONF_PATH}"
        )

    log.action(WSL_CONF_PATH)
    if files.install_to(ctx, "wsl.conf", WSL_CONF_PATH, WSL_CONF_MODE, backup=True):
        log.ok(f"{WSL_CONF_PATH} updated")
        log.warn(
            "Run 'wsl --shutdown' from Windows PowerShell to apply "
            "(not from inside this session)"
        )


if __name__ == "__main__":
    run(install, "WSL Conf")
