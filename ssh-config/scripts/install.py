#!/usr/bin/env python3
"""Remote installer for the ssh-config module (freender/homelab-ops#30).

The one module so far that does **not** run as root. Its target is the deploy
user's own `~/.ssh/config`, so running it as root would drop root-owned files
into a user's home and break the next deploy -- the orchestrator has always
staged it with `require_root=False`, and `run()` now takes the same flag.

`~/.ssh/config` cannot come from a file map. The orchestrator knows
`config.user` but not whether that user's home is `/home/<user>` or `/root`, so
the destination is resolved here, on the host, exactly as the bash `~` did.

The 700 on `~/.ssh` is load-bearing rather than tidiness: ssh ignores a config
in a directory it considers too open, so creating it under the default umask
would be a deploy that reports success and changes nothing.
"""

from __future__ import annotations

from pathlib import Path

from homelab_install import files, log, run
from homelab_install.context import InstallContext

SSH_DIR_MODE = "700"
CONFIG_MODE = "600"


def install(ctx: InstallContext) -> None:
    log.header(f"Installing SSH config on {ctx.host}")

    ssh_dir = Path.home() / ".ssh"
    files.ensure_dir(ctx, str(ssh_dir), SSH_DIR_MODE)

    # Backed up because this is the file that, wrong, locks you out of the host
    # you would fix it from.
    files.install_to(ctx, "config", str(ssh_dir / "config"), CONFIG_MODE, backup=True)


if __name__ == "__main__":
    run(install, "SSH Config", require_root=False)
