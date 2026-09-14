#!/usr/bin/env python3
"""Remote installer for the apt-upgrade module (freender/homelab-ops#30, third port).

Picked third by #35(d) for the surface the first two never touched: the
`build/<host>/env` file, `require_env`, `homelab_apply_pause`, and timer units.
All four are now library calls -- `env.require`/`env.flag`, `systemd.pause`,
`systemd.ensure_running` against a `.timer`.

Two behaviours the bash had that are worth keeping in view while reading this:

* **The drop-in is removed, not just skipped, when `auto_reboot` is false.** That
  is the whole of the flag's reversibility. A host that had it set and then had
  it taken away must stop rebooting itself, and that only happens if the file
  comes off.
* **Pause forces `auto_reboot` off too.** Pausing means the host stops acting on
  its own, and rebooting itself is acting on its own. Resuming redeploys it.

The policy check after writing the drop-in reads `apt-config dump`, not the file
just written: APT merges all of `apt.conf.d` in order, so a later fragment can
still override this one. Verifying the file would confirm only that we wrote what
we meant to write, which was never in doubt.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

SERVICE_NAME = "homelab-apt-dist-upgrade.service"
TIMER_NAME = "homelab-apt-dist-upgrade.timer"
TIMER_PATH = f"/etc/systemd/system/{TIMER_NAME}"
AUTO_REBOOT_PATH = "/etc/apt/apt.conf.d/53homelab-auto-reboot"
AUTO_REBOOT_CONF = "auto-reboot.conf"
REBOOT_REQUIRED = "/var/run/reboot-required"

DEFAULT_SCHEDULE = "*-*-* 09:00:00"

# Indirection point for tests, matching packages.py / systemd.py.
_run = subprocess.run


def apply_auto_reboot(ctx: InstallContext, auto_reboot: bool) -> None:
    """Install or remove the unattended-upgrades reboot drop-in.

    Unattended reboot is delegated to unattended-upgrades, which is already
    present and timer-driven on these hosts -- this only sets the reboot keys it
    leaves unset by default.
    """
    if not auto_reboot:
        files.remove(ctx, AUTO_REBOOT_PATH, reason="auto_reboot disabled")
        return

    if not packages.installed(ctx, "unattended-upgrades"):
        log.sub("It supplies the reboot mechanism; this module only sets its keys.")
        raise InstallError("auto_reboot requires unattended-upgrades, which is not installed")

    files.install(ctx, AUTO_REBOOT_CONF)

    dump = _run(
        ["apt-config", "dump", "Unattended-Upgrade::Automatic-Reboot"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout or ""
    if 'Automatic-Reboot "true"' not in dump:
        log.sub(dump.strip() or "<apt-config returned nothing>")
        raise InstallError("resolved Unattended-Upgrade::Automatic-Reboot is not true")

    # The reboot only ever happens at the end of an unattended-upgrades run, so
    # the timer that invokes it is a hard dependency of this feature.
    if _run(
        ["systemctl", "is-enabled", "--quiet", "apt-daily-upgrade.timer"], check=False
    ).returncode != 0:
        raise InstallError("apt-daily-upgrade.timer is not enabled; auto_reboot would never fire")

    log.ok(f"Unattended-upgrades will reboot when {REBOOT_REQUIRED} is present")


def report_reboot_required(auto_reboot: bool) -> None:
    """Mirror the bash's `$(hostname)`, not `ctx.host`.

    They are not the same string: `ctx.host` is the inventory name, and for hosts
    whose `config.hostname` differs (deepstone answers to `timemachine`) the
    inventory name is not what an operator would grep the host's own logs for.
    """
    if not Path(REBOOT_REQUIRED).is_file():
        return
    hostname = socket.gethostname()
    if auto_reboot:
        log.sub(f"Reboot required on {hostname}; unattended-upgrades will take it")
    else:
        log.warn(f"Reboot required on {hostname}")


def install(ctx: InstallContext) -> None:
    log.header("Apt Upgrade")

    # AUTOUPGRADE and PAUSED decide whether this host upgrades itself and whether
    # it may reboot; a truncated env file that silently defaulted them to false
    # would disable the feature rather than fail.
    env.require(ctx, "AUTOUPGRADE", "PAUSED", "AUTO_REBOOT")
    autoupgrade = env.flag(ctx, "AUTOUPGRADE")
    paused = env.flag(ctx, "PAUSED")
    auto_reboot = env.flag(ctx, "AUTO_REBOOT")
    schedule = env.text(ctx, "SCHEDULE", DEFAULT_SCHEDULE)

    # The service unit is installed unconditionally: it is what both the timer
    # and the on-demand path invoke. The reload has to happen here rather than
    # being left to `ensure_running` below, because two of the three paths out of
    # this function act on the unit without going through it -- the pause path
    # disables around it, and the on-demand path starts it directly. Starting a
    # just-rewritten unit without a reload runs the cached definition.
    if files.install(ctx, "service"):
        systemd.daemon_reload(ctx)

    if paused:
        systemd.pause(ctx, TIMER_NAME)
        apply_auto_reboot(ctx, auto_reboot=False)
        log.header("apt-upgrade paused")
        report_reboot_required(auto_reboot=False)
        return

    # Applied before the upgrade runs, so a dist-upgrade started below that
    # installs a kernel already has the reboot policy in place.
    apply_auto_reboot(ctx, auto_reboot)

    if autoupgrade:
        files.install(ctx, "timer")
        log.sub(f"Timer schedule: {schedule}")
        systemd.ensure_running(ctx, TIMER_NAME, changed=ctx.changes.touched("service", "timer"))
    else:
        # Autoupgrade not enabled: retire any previous timer, then run once now.
        systemd.retire_unit(ctx, TIMER_NAME, TIMER_PATH)
        log.sub("Running apt upgrade now (autoupgrade not enabled)...")
        systemd.run_once(ctx, SERVICE_NAME)
        log.sub("Upgrade complete")

    report_reboot_required(auto_reboot)


if __name__ == "__main__":
    run(install, "Apt Upgrade")
