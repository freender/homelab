#!/usr/bin/env python3
"""Remote installer for the apcupsd module (freender/homelab-ops#30).

Every destination comes from `build/<host>/file-map.conf`, which the orchestrator
derives from the same `FileSpec`s it diffs against, so the dry-run and the install
cannot disagree about where a file goes.

Three roles. `master` (bray) owns the UPS over USB and serves NIS; `slave` (ace,
clovis) reads it over the network; `master-standalone` (osiris) owns its own UPS
and is not in the cluster. The two cluster roles also get
`homelab-ha-rearm.service`, which re-arms Proxmox HA at boot after a coordinated
UPS shutdown.

Worth knowing before reading:

* **An unknown role fails.** The bash defaulted `ROLE="unknown"` before sourcing
  the env file and sent anything that was not `master`/`slave` down the
  standalone branch -- so a truncated env file on a cluster node *retired* its HA
  re-arm and reported success.
* **The HA re-arm is enabled, never started.** Starting it runs
  `ha-manager crm-command arm-ha`, which on a cluster deliberately disarmed for
  maintenance is exactly the wrong thing for a config deploy to do.
* **Only `apcupsd.conf` restarts the daemon.** `apccontrol` looks `doshutdown` up
  and executes it afresh on every event, so a changed hook is live without a
  restart. The bash restarted on either; a restart on a slave briefly drops its
  view of the master's UPS, which is not worth paying for a file nobody caches.
* **`doshutdown` must be executable.** `apccontrol` runs it only if `-x`, and
  otherwise falls through to its default shutdown -- which on a slave is the
  duplicate shutdown path the hook exists to suppress. The file map pins 755.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

SERVICE = "apcupsd"
ROLES = ("master", "slave", "master-standalone")
HA_ROLES = frozenset({"master", "slave"})
# Roles whose apcupsd serves NIS locally, so `apcaccess` has something to query.
# Slaves run `NETSERVER off` and would only ever report a connection failure.
NIS_ROLES = frozenset({"master", "master-standalone"})

APCUPSD_FILES = ("apcupsd.conf", "doshutdown")
HA_REARM_SCRIPT = "homelab-ha-rearm"
HA_REARM_UNIT = "homelab-ha-rearm.service"
HA_REARM_SCRIPT_PATH = "/usr/local/sbin/homelab-ha-rearm"
HA_REARM_UNIT_PATH = f"/etc/systemd/system/{HA_REARM_UNIT}"

APCCONTROL = "/etc/apcupsd/apccontrol"
DEFAULTS_FILE = "/etc/default/apcupsd"

_run = subprocess.run


def read_role(ctx: InstallContext) -> str:
    env.require(ctx, "ROLE")
    role = ctx.env["ROLE"]
    if role not in ROLES:
        raise InstallError(f"unknown apcupsd role {role!r}; expected one of {', '.join(ROLES)}")
    return role


def install_ha_rearm(ctx: InstallContext) -> None:
    changed = files.install(ctx, HA_REARM_SCRIPT)
    if files.install(ctx, HA_REARM_UNIT):
        changed = True
    if changed:
        systemd.daemon_reload(ctx)
    systemd.enable(ctx, HA_REARM_UNIT)


def retire_ha_rearm(ctx: InstallContext) -> None:
    """Take the re-arm off a host that is not a cluster member. A no-op on osiris,
    which never had it; the branch exists for a node moved out of the cluster."""
    systemd.retire_unit(ctx, HA_REARM_UNIT, HA_REARM_UNIT_PATH)
    files.remove(ctx, HA_REARM_SCRIPT_PATH, reason="not a cluster role")


def ensure_apccontrol_executable() -> None:
    """`apccontrol` is the package's event dispatcher; a non-executable one means
    no hook and no shutdown at all. Missing means a broken package, so fail."""
    path = Path(APCCONTROL)
    if not path.is_file():
        raise InstallError(f"missing {APCCONTROL}; the apcupsd package looks broken")
    mode = path.stat().st_mode
    if mode & 0o111 != 0o111:
        path.chmod(mode | 0o111)
        log.sub(f"Made {APCCONTROL} executable")


def mark_configured() -> None:
    """Flip Debian's `ISCONFIGURED=no` guard, which stops the init path from
    starting apcupsd at all. Absent on packages that no longer ship it."""
    path = Path(DEFAULTS_FILE)
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    updated = [
        "ISCONFIGURED=yes\n" if line.rstrip("\n") == "ISCONFIGURED=no" else line
        for line in lines
    ]
    if updated != lines:
        path.write_text("".join(updated), encoding="utf-8")
        log.sub(f"Set ISCONFIGURED=yes in {DEFAULTS_FILE}")


def report_status(role: str) -> None:
    """Informational only: a UPS that has not answered yet is not a failed deploy."""
    if role not in NIS_ROLES:
        log.sub("Slave role: no local NIS server; UPS status is on the master")
        return
    result = _run(["apcaccess", "status"], check=False, capture_output=True, text=True)
    wanted = ("STATUS", "MODEL", "TIMELEFT", "BCHARGE")
    # Right after a restart apcaccess answers before it has polled the UPS, with
    # the keys present and the values blank (`STATUS   :`). Found on the osiris
    # canary; a blank field is "not yet", not a status.
    lines = [
        line
        for line in (result.stdout or "").splitlines()
        if line.startswith(wanted) and line.partition(":")[2].strip()
    ]
    if result.returncode != 0 or not lines:
        log.sub("Waiting for UPS connection...")
        return
    for line in lines:
        log.sub(line.rstrip())


def install(ctx: InstallContext) -> None:
    role = read_role(ctx)
    log.header(f"apcupsd {role}")

    packages.ensure(ctx, "apcupsd")

    for name in APCUPSD_FILES:
        files.install(ctx, name, backup=name == "apcupsd.conf")

    if role in HA_ROLES:
        install_ha_rearm(ctx)
    else:
        retire_ha_rearm(ctx)

    ensure_apccontrol_executable()
    mark_configured()

    systemd.ensure_running(ctx, SERVICE, changed=ctx.changes.touched("apcupsd.conf"))
    report_status(role)


if __name__ == "__main__":
    run(install, "apcupsd")
