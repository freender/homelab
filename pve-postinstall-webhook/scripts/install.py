#!/usr/bin/env python3
"""Remote installer for the pve-postinstall-webhook module (freender/homelab-ops#30).

Installs the PDM installation poller on `arc` plus the root SSH agent and the
1Password key loader it deploys through. Nothing here is rendered per host except
`poller.env`; the scripts and units are static files staged from `scripts/`.

**`poller.env` carries a live API token.** It is the only ported module whose
`build/<host>/env` is a secret: the orchestrator renders it into tmpfs and stages
it straight onto the host at mode 600. Two consequences worth knowing while
reading:

* It is installed through `files.install_to` rather than copied unconditionally,
  so a redeploy that did not rotate the token does not rewrite the file. The bash
  wrote it every run.
* `run()` parses it into `ctx.env` like any other env file, and nothing here reads
  a value out of it. That is deliberate -- the file's destination is the whole
  contract, and reading a token in order to re-render it would put it somewhere a
  traceback could reach.

**A changed unit file is now restarted.** The bash ran `systemctl enable --now` on
each unit, which starts a stopped unit and does nothing at all to a running one --
so an edited `homelab-ssh-agent.service` stayed running under its old definition
until somebody noticed. `systemd.ensure_running` restarts on change. The agent
restart drops the keys it was holding, which is safe only because
`homelab-op-ssh-load.service` runs immediately afterwards and reloads them; that
ordering is load-bearing, not incidental.

**The retirement of the old HTTP listener is not ported.** `babd66b` (2026-08-31)
added a block to disable `homelab-postinstall-webhook.service` and delete
`/usr/local/sbin/homelab-postinstall-webhook` and
`/etc/homelab-postinstall-webhook/env`. It has already run: none of the three
exists on `arc`, which is the only host with this feature, and a host built fresh
by `pve-autoinstall` would never have had them. Carrying a completed migration
forward as permanent dead code is how an installer gets to 500 lines.
"""

from __future__ import annotations

from homelab_install import files, log, packages, run, systemd
from homelab_install.context import InstallContext

# `util-linux` is Essential and can never actually be missing; it is listed so the
# set of packages this module depends on is stated in one place rather than being
# inferred from which binaries the deploy script happens to call.
PACKAGES = ("git", "openssh-client", "util-linux", "python3-yaml")

# Mode is pinned per directory rather than left to the umask. `events` holds
# deploy payloads and `/root/.local/bin` holds the 1Password loader, so both are
# 700; `state` is 755 because nothing in it is sensitive and the poller reads it
# from unit contexts that need no privilege.
DIRECTORIES = (
    ("/etc/homelab-postinstall-webhook", "700"),
    ("/var/lib/homelab-postinstall-webhook/events", "700"),
    ("/var/lib/homelab-postinstall-webhook/state", "755"),
    ("/root/.local/bin", "700"),
    ("/root/.config", "700"),
)

POLLER_SCRIPTS = (
    ("homelab-pdm-installation-watch.py", "/usr/local/sbin/homelab-pdm-installation-watch", "755"),
    ("homelab-pdm-refresh-remote.py", "/usr/local/sbin/homelab-pdm-refresh-remote", "755"),
    ("homelab-postinstall-deploy.sh", "/usr/local/sbin/homelab-postinstall-deploy", "755"),
)

# The agent's environment file is 600: it names the socket path and the service
# account token file, which together are the whole of how root reaches 1Password.
AGENT_FILES = (
    ("op-ssh-add", "/root/.local/bin/op-ssh-add", "700"),
    ("addhomelabkeys", "/root/.local/bin/addhomelabkeys", "700"),
    ("op-ssh-agent.conf", "/root/.config/op-ssh-agent.env", "600"),
)

UNIT_DIR = "/etc/systemd/system"
UNIT_MODE = "644"
UNITS = (
    "homelab-pdm-installation-watch.service",
    "homelab-pdm-installation-watch.timer",
    "homelab-ssh-agent.service",
    "homelab-op-ssh-load.service",
    "homelab-op-ssh-load.timer",
)

# `homelab-pdm-installation-watch.service` is deliberately absent: it is a oneshot
# the timer invokes, and enabling it would make it run at boot as well.
ENABLED_UNITS = (
    "homelab-pdm-installation-watch.timer",
    "homelab-ssh-agent.service",
    "homelab-op-ssh-load.timer",
)
KEY_LOAD_UNIT = "homelab-op-ssh-load.service"

POLLER_ENV_NAME = "env"
POLLER_ENV_DEST = "/etc/homelab-postinstall-webhook/poller.env"


def install_staged(ctx: InstallContext, entries: tuple[tuple[str, str, str], ...]) -> None:
    """Install `(name, dest, mode)` entries from the staged `scripts/` directory.

    These are static files with no per-host render, so there is no `build/<host>/`
    to look them up in and no file map to describe them -- `files.install_from`
    against `script_dir/scripts` is the right depth.
    """
    for name, dest, mode in entries:
        files.install_from(ctx, ctx.script_dir / "scripts" / name, dest, mode, record=name)


def install_units(ctx: InstallContext) -> bool:
    """Install every unit file. Returns True if any of them changed."""
    changed = False
    for unit in UNITS:
        if files.install_from(
            ctx, ctx.script_dir / "scripts" / unit, f"{UNIT_DIR}/{unit}", UNIT_MODE, record=unit
        ):
            changed = True
    return changed


def install(ctx: InstallContext) -> None:
    log.header("PVE Post-Install Poller")

    packages.ensure(ctx, *PACKAGES)

    for path, mode in DIRECTORIES:
        files.ensure_dir(ctx, path, mode)

    log.action("Installing PDM poller and deploy scripts")
    install_staged(ctx, POLLER_SCRIPTS)

    log.action("Installing root SSH agent + 1Password key loader")
    install_staged(ctx, AGENT_FILES)

    log.action("Installing PDM poller config")
    files.install_to(ctx, POLLER_ENV_NAME, POLLER_ENV_DEST, "600")

    log.action("Installing retained systemd units")
    # Reloaded here rather than left to `ensure_running`, because the key-load
    # service below is started by `run_once` and never passes through it: a change
    # to that unit alone would otherwise be started from systemd's cached copy.
    if install_units(ctx):
        systemd.daemon_reload(ctx)

    for unit in ENABLED_UNITS:
        systemd.ensure_running(ctx, unit, changed=ctx.changes.touched(unit))

    # Loading the keys is what makes the poller able to deploy at all, and the
    # unit is a oneshot, so `start` blocks until it has finished and its exit
    # status is the load's. A deploy that installed the loader and left the agent
    # empty would look successful and poll forever without deploying anything.
    log.action("Loading SSH keys from 1Password")
    systemd.run_once(ctx, KEY_LOAD_UNIT)

    log.ok("PDM post-install poller installed")
    log.sub("PDM watch: systemctl list-timers homelab-pdm-installation-watch.timer --no-pager")
    log.sub("SSH agent: systemctl status homelab-ssh-agent.service --no-pager")
    log.sub("SSH key load: systemctl list-timers homelab-op-ssh-load.timer --no-pager")


if __name__ == "__main__":
    run(install, "PVE Post-Install Poller")
