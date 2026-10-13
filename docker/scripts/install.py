#!/usr/bin/env python3
"""Remote installer for the docker module (freender/homelab-ops#30, #151).

Installs Docker Engine from download.docker.com with its daemon config, then the
appdata helper scripts (`start.sh`, `rm.sh`, `rebuild.sh`, `docker-common.sh`),
the env file they read, and -- on hosts with a `docker.update_schedule` -- the
`homelab-docker-update` service and timer. Every destination except the apt
source comes from `build/<host>/file-map.conf`, derived from the same
`FileSpec`s the dry-run diffs against.

Worth knowing before reading:

* **This is the only owner of the engine.** `ubuntu-setup` used to run
  `get.docker.com` on cinci and cottonwood; everything else was installed by
  hand. The key, the source line, the package set, `daemon.json` and both
  drop-ins are what every host already ran when this took over (#151).
* **The apt source is written on the host**, from `/etc/os-release` and
  `dpkg --print-architecture`. A release upgrade is what makes it go stale:
  cottonwood moved to resolute and was left with a disabled `noble`
  `docker.sources`, which the old "does a source file exist" check passed.
* **The config is written before the packages are installed**, so a fresh host
  starts dockerd with it and needs no restart.
* **A running daemon is never restarted.** Restarting dockerd bounces every
  container on the host, and on the HA hosts that is a failover. A changed
  `daemon.json` or drop-in is written, systemd is reloaded, and the deploy warns.
  `apt-upgrade` restarts it anyway on the next engine update.
* **No package is pinned or removed.** `apt-upgrade` keeps the engine current,
  and `docker-model-plugin` (installed by `get.docker.com` on some hosts) is left
  wherever it is.
* **The helper scripts are pinned to 755.** The bash only ever `chmod +x`ed them,
  which adds execute and never removes write, so they had drifted to 775 and 777
  on several hosts. `start.sh` is the update service's `ExecStart` and runs as
  root; a group- or world-writable copy of it is root for whoever can write it.
* **A disabled timer keeps its unit files.** Same as the bash: the orchestrator
  stops rendering them and this stops the timer, but nothing is removed.
* **A redeploy retries a failed update run** -- see `systemd.recover_failed` for
  why that is the right moment and never a failed deploy.
"""

from __future__ import annotations

import grp
import pwd
import shutil
import subprocess
from pathlib import Path

from homelab_install import env, files, log, osinfo, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

UPDATE_SERVICE = "homelab-docker-update.service"
UPDATE_TIMER = "homelab-docker-update.timer"
HELPER_FILES = ("start.sh", "rm.sh", "rebuild.sh", "docker-common.sh", "env")
TIMER_FLAG = "ENABLE_DOCKER_UPDATE_TIMER"
GROUP_USER = "DOCKER_GROUP_USER"

KEYRING_FILE = "docker.asc"
DAEMON_CONFIG = "daemon.json"
DROP_INS = ("docker-timeout.conf", "containerd-override.conf")
ENGINE_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
    "docker-ce-rootless-extras",
)
ENGINE_UNITS = ("containerd.service", "docker.service")
SUPPORTED_DISTROS = ("debian", "ubuntu")

# Module-level so tests can rebind them under tmp_path.
OS_RELEASE = osinfo.OS_RELEASE
DOCKER_LIST = "/etc/apt/sources.list.d/docker.list"
# deb822 spelling of the same repo, left by `get.docker.com` on newer releases.
# Two sources for one repo make apt warn, and a disabled one hides a stale suite.
STALE_SOURCES = ("/etc/apt/sources.list.d/docker.sources",)
BACKUP_DIR = "/var/backups/homelab/docker"

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which


def _check(argv: list[str]) -> None:
    result = _run(argv, check=False, stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed (exit {result.returncode})")


def preflight(ctx: InstallContext) -> tuple[bool, str]:
    """Every refusal, before any write. Returns (timer enabled, apt source line)."""
    # Required, not defaulted: an absent flag must not read as "turn the timer off".
    env.require(ctx, TIMER_FLAG, GROUP_USER)
    timer_enabled = env.flag(ctx, TIMER_FLAG)
    user = ctx.env[GROUP_USER]
    try:
        pwd.getpwnam(user)
    except KeyError as exc:
        raise InstallError(f"docker group user {user} does not exist") from exc
    return timer_enabled, apt_source_line(ctx)


def apt_source_line(ctx: InstallContext) -> str:
    """The `deb` line for this host. `signed-by` is wherever the file map puts the
    key, so the two cannot name different paths."""
    try:
        keyring, _mode = ctx.file_map[KEYRING_FILE]
    except KeyError as exc:
        raise InstallError(f"missing file-map entry: {KEYRING_FILE}") from exc
    release = osinfo.os_release(OS_RELEASE)
    distro = release.get("ID", "")
    codename = release.get("VERSION_CODENAME", "")
    if distro not in SUPPORTED_DISTROS or not codename:
        raise InstallError(
            f"unsupported OS for the Docker apt repo: ID={distro!r} VERSION_CODENAME={codename!r}"
        )
    result = _run(["dpkg", "--print-architecture"], check=False, capture_output=True, text=True)
    arch = (result.stdout or "").strip()
    if result.returncode != 0 or not arch:
        raise InstallError("dpkg --print-architecture failed")
    return (
        f"deb [arch={arch} signed-by={keyring}] "
        f"https://download.docker.com/linux/{distro} {codename} stable\n"
    )


def remove_snap_docker() -> None:
    if (
        not _which("snap")
        or _run(["snap", "list", "docker"], check=False, capture_output=True).returncode
    ):
        return
    log.sub("Removing snap docker before installing Docker CE...")
    _run(["snap", "stop", "docker"], check=False, capture_output=True)
    _run(["snap", "disable", "docker"], check=False, capture_output=True)
    _check(["snap", "remove", "--purge", "docker"])
    log.ok("Removed snap docker")


def ensure_apt_source(ctx: InstallContext, line: str) -> bool:
    """Write the one-line source and retire any other spelling of it. Returns True
    if apt's view of the repo changed."""
    staged = ctx.build_dir / "docker.list"
    staged.write_text(line, encoding="utf-8")
    changed = files.install_from(ctx, staged, DOCKER_LIST, "644")
    for stale in STALE_SOURCES:
        path = Path(stale)
        if not path.exists():
            continue
        backup = Path(BACKUP_DIR)
        backup.mkdir(parents=True, exist_ok=True)
        (backup / path.name).write_bytes(path.read_bytes())
        changed |= files.remove(ctx, stale, f"superseded by {DOCKER_LIST}; copy in {BACKUP_DIR}")
    return changed


def ensure_engine(ctx: InstallContext, source_line: str) -> None:
    remove_snap_docker()

    sources = [files.install(ctx, KEYRING_FILE), ensure_apt_source(ctx, source_line)]
    if any(sources):
        packages.sources_changed(ctx)

    config = files.install(ctx, DAEMON_CONFIG)
    drop_ins = any([files.install(ctx, name) for name in DROP_INS])
    if drop_ins:
        systemd.daemon_reload(ctx)

    fresh = not packages.installed(ctx, "docker-ce")
    packages.ensure(ctx, *ENGINE_PACKAGES)
    for unit in ENGINE_UNITS:
        systemd.ensure_running(ctx, unit, changed=False)

    if (config or drop_ins) and not fresh:
        log.warn(
            "Docker daemon config changed; restart containerd and docker to apply "
            "(bounces every container on this host)"
        )


def ensure_docker_group(user: str) -> None:
    if user == "root":
        log.sub("root needs no docker group membership")
        return
    try:
        group = grp.getgrnam("docker")
    except KeyError as exc:
        raise InstallError("docker group does not exist after installing Docker CE") from exc
    if user in group.gr_mem or pwd.getpwnam(user).pw_gid == group.gr_gid:
        log.sub(f"{user} already in docker group")
        return
    _check(["usermod", "-aG", "docker", user])
    log.ok(f"Added {user} to docker group")


def install(ctx: InstallContext) -> None:
    timer_enabled, source_line = preflight(ctx)
    log.header("Docker")

    log.action("Docker engine")
    ensure_engine(ctx, source_line)
    ensure_docker_group(ctx.env[GROUP_USER])

    log.action("Helper scripts")
    for name in HELPER_FILES:
        files.install(ctx, name)

    if not timer_enabled:
        systemd.ensure_stopped(ctx, UPDATE_TIMER)
        return

    # Both, never short-circuited: a changed timer must not skip the service.
    changed = [files.install(ctx, unit) for unit in (UPDATE_SERVICE, UPDATE_TIMER)]
    systemd.ensure_running(ctx, UPDATE_TIMER, changed=any(changed))
    systemd.recover_failed(ctx, UPDATE_SERVICE)


if __name__ == "__main__":
    run(install, "Docker")
