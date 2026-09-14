#!/usr/bin/env python3
"""Remote installer for the zfs-automation module (freender/homelab-ops#30).

Installs sanoid's config, the snapshot and scrub scripts and units, one syncoid
script, service and timer per replication job, the known-hosts refresh helper, the
source private keys, and on a push target the receive-only wrapper, its dataset
allow-list, authorized_keys and `zfs allow` grants. Then it sets every managed
timer to the state the host's config asks for.

Behaviour changes from `install.sh`:

* **Every refusal comes before any write.** That covers the env file, every flag,
  every staged file-map entry, and on a push target the `zfs allow` plan: a dataset
  with no existing parent now refuses before anything is installed. The bash found
  that out after it had created the push user and installed the wrapper and keys.
* **A flag typo fails the deploy** (`env.flag`). The bash compared against `true`,
  so `ENABLE_ZFS_REPLICATION=ture` stopped every replication timer.
* **A timer is restarted only when its own unit file changed.** The bash restarted
  every enabled timer whenever any unit, script or config in the module changed.
* **An enabled timer that is not running is started** (`systemd.ensure_running`).
  The bash reported it as "already enabled" and left it stopped.
* **A retired replication job's units go through `systemd.retire_unit`**, which
  also stops a running service and clears its failed record. The bash disabled only
  an *enabled* timer and deleted the files, so a stopped-but-failed job stayed in
  `systemctl --failed` with no unit file behind it.
* **Failed-replication recovery never blocks or fails the deploy.** It uses
  `systemd.recover_failed`, which waits at most 300s. The bash ran a bare
  `systemctl start` on a oneshot, so the deploy waited for the whole replication,
  and a replication that failed again failed the deploy.
* **The known-hosts refresh helper runs only when the file map carries it.** The
  bash ran whatever was executable at that path.
* **The `zfs-automation-managed` shadow copy is removed, not maintained.** The bash
  copied every rendered file into `$HOMELAB_STATE_DIR/zfs-automation-managed/` on
  every deploy. Nothing on any host reads that directory, and it held a second copy
  of cinci's push `authorized_keys`.

**Not ported.** Each was checked and found absent on all six hosts (ace, bray,
clovis, osiris, cinci, cottonwood): the legacy single replication unit, the retired
`homelab-zfs-health-check` units and script, the legacy
`$HOMELAB_STATE_DIR/zfs-automation` rebuild bundle, and the pull-source access files
(datasets conf, `homelab-zfs-send-only`, `authorized_keys`). The `zfs-pull` user and
its empty home still exist on bray, clovis and cottonwood; the bash never removed
those either.
"""

from __future__ import annotations

import pwd
import shutil
import subprocess
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run


def _user_exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


SYSTEMD_DIR = "/etc/systemd/system"
BIN_DIR = "/usr/local/bin"
ETC_HOMELAB = "/etc/homelab"
PUSH_DATASETS_CONF = "/etc/homelab/zfs-push-datasets.conf"
RECEIVE_ONLY = "/usr/local/sbin/homelab-zfs-receive-only"

SNAPSHOT_TIMER = "homelab-zfs-snapshots.timer"
SCRUB_TIMER = "zfs-scrub.timer"
REPLICATION_PREFIX = "homelab-zfs-replication-"
KNOWN_HOSTS_REFRESH = "homelab-zfs-refresh-known-hosts.sh"
PUSH_ENTRIES = ("homelab-zfs-receive-only.sh", "zfs-push-datasets.conf", "zfs-push-authorized-keys")
BASE_ENTRIES = (
    "sanoid.conf",
    "homelab-zfs-snapshots.service",
    SNAPSHOT_TIMER,
    "homelab-zfs-snapshots.sh",
    "homelab-zfs-scrub.sh",
    "zfs-scrub.service",
    SCRUB_TIMER,
)
PACKAGES = ("sanoid", "lzop", "mbuffer", "pv")
PUSH_PERMISSIONS = "create,mount,receive,hold,release"

# PAUSED_REPLICATION_TIMERS is legitimately empty, so it is checked for presence
# separately rather than through `env.require`.
REQUIRED_ENV = (
    "HOMELAB_STATE_DIR",
    "PAUSED",
    "ENABLE_ZFS_SNAPSHOTS",
    "ENABLE_ZFS_REPLICATION",
    "ZFS_REPLICATION_RECOVERY_START_FAILED",
    "ENABLE_ZFS_SCRUB",
    "ENABLE_ZFS_PUSH_TARGET",
    "ZFS_PUSH_TARGET_USER",
    "ZFS_PUSH_TARGET_HOME",
)
FLAGS = (
    "PAUSED",
    "ENABLE_ZFS_SNAPSHOTS",
    "ENABLE_ZFS_REPLICATION",
    "ZFS_REPLICATION_RECOVERY_START_FAILED",
    "ENABLE_ZFS_SCRUB",
    "ENABLE_ZFS_PUSH_TARGET",
)


def _dataset_exists(dataset: str) -> bool:
    result = _run(["zfs", "list", "-H", "-o", "name", dataset], check=False, capture_output=True)
    return result.returncode == 0


def plan_push_grants(ctx: InstallContext) -> list[tuple[str, bool]]:
    """`(dataset, descendants_only)` per line of the staged allow-list.

    A dataset that exists is granted directly. One that does not yet exist is
    granted as future descendants of its nearest existing parent, so the first
    push can create it.
    """
    grants: list[tuple[str, bool]] = []
    staged = ctx.build_dir / "zfs-push-datasets.conf"
    for dataset in staged.read_text(encoding="utf-8").split():
        if _dataset_exists(dataset):
            grants.append((dataset, False))
            continue
        parent = dataset
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if _dataset_exists(parent):
                grants.append((parent, True))
                break
        else:
            raise InstallError(f"ZFS push target dataset parent not found: {dataset}")
    return grants


def preflight(ctx: InstallContext) -> list[tuple[str, bool]]:
    """Every check that can refuse the deploy. Returns the push grants to apply."""
    env.require(ctx, *REQUIRED_ENV)
    if "PAUSED_REPLICATION_TIMERS" not in ctx.env:
        raise InstallError(
            f"incomplete env file at {ctx.build_dir / 'env'} "
            "(missing: PAUSED_REPLICATION_TIMERS); refusing to run with an ambiguous config"
        )
    for name in FLAGS:
        env.flag(ctx, name)

    required = list(BASE_ENTRIES)
    if env.flag(ctx, "ENABLE_ZFS_PUSH_TARGET"):
        required += PUSH_ENTRIES
    unmapped = [name for name in required if name not in ctx.file_map]
    if unmapped:
        raise InstallError(f"missing file-map entries: {', '.join(unmapped)}")
    missing = [name for name in ctx.file_map if not (ctx.build_dir / name).is_file()]
    if missing:
        raise InstallError(f"missing staged files: {', '.join(missing)}")

    if env.flag(ctx, "ENABLE_ZFS_PUSH_TARGET"):
        return plan_push_grants(ctx)
    return []


def remove_shadow_copy(ctx: InstallContext) -> None:
    managed = Path(ctx.env["HOMELAB_STATE_DIR"]) / "zfs-automation-managed"
    if managed.is_dir():
        shutil.rmtree(managed)
        log.ok(f"Removed unread shadow copy at {managed}")


def replication_timers(ctx: InstallContext) -> list[str]:
    return [
        name
        for name in ctx.file_map
        if name.startswith(REPLICATION_PREFIX) and name.endswith(".timer")
    ]


def retire_obsolete_replication(ctx: InstallContext) -> bool:
    """Remove every replication unit and script the file map no longer carries.

    Timers go first so a retired job cannot fire between its service being removed
    and its timer. Returns True if anything was removed.
    """
    mapped = {dest for dest, _mode in ctx.file_map.values()}
    systemd_dir = Path(SYSTEMD_DIR)
    retired = False
    for suffix in (".timer", ".service"):
        for path in sorted(systemd_dir.glob(f"{REPLICATION_PREFIX}*{suffix}")):
            if str(path) not in mapped:
                retired |= systemd.retire_unit(ctx, path.name, str(path))
    for path in sorted(Path(BIN_DIR).glob(f"{REPLICATION_PREFIX}*")):
        if str(path) not in mapped:
            retired |= files.remove(ctx, str(path), "replication job retired")
    return retired


def prepare_push_user(ctx: InstallContext) -> None:
    user = ctx.env["ZFS_PUSH_TARGET_USER"]
    home = ctx.env["ZFS_PUSH_TARGET_HOME"]
    if not _user_exists(user):
        _run(
            [
                "useradd",
                "--system",
                "--home-dir",
                home,
                "--create-home",
                "--shell",
                "/bin/bash",
                user,
            ],
            check=True,
        )
        log.ok(f"Created {user} user")
    files.ensure_dir(ctx, f"{home}/.ssh", "700")
    Path(ETC_HOMELAB).mkdir(parents=True, exist_ok=True)


def remove_push_access(ctx: InstallContext) -> None:
    """Take push access off a host that is no longer a push target."""
    home = ctx.env["ZFS_PUSH_TARGET_HOME"]
    for path in (PUSH_DATASETS_CONF, RECEIVE_ONLY, f"{home}/.ssh/authorized_keys"):
        files.remove(ctx, path, "no longer a push target")


def grant_push_access(ctx: InstallContext, grants: list[tuple[str, bool]]) -> None:
    user = ctx.env["ZFS_PUSH_TARGET_USER"]
    home = ctx.env["ZFS_PUSH_TARGET_HOME"]
    _run(["chown", "-R", f"{user}:{user}", home], check=True)
    for dataset, descendants_only in grants:
        scope = ["-d"] if descendants_only else []
        _run(["zfs", "allow", *scope, "-u", user, PUSH_PERMISSIONS, dataset], check=True)
        if descendants_only:
            log.sub(f"Granted future descendant receive access at {dataset}")
        else:
            log.ok(f"Granted receive target access for {dataset}")


def refresh_known_hosts(ctx: InstallContext) -> None:
    if KNOWN_HOSTS_REFRESH not in ctx.file_map:
        return
    log.action("SSH known_hosts refresh")
    helper = ctx.file_map[KNOWN_HOSTS_REFRESH][0]
    result = _run([helper], check=False)
    if result.returncode != 0:
        raise InstallError(f"{helper} failed (exit {result.returncode})")


def set_timer(ctx: InstallContext, timer: str, enabled: bool) -> None:
    if enabled:
        systemd.ensure_running(ctx, timer, changed=ctx.changes.touched(timer))
    else:
        systemd.ensure_stopped(ctx, timer)


def install(ctx: InstallContext) -> None:
    log.header("ZFS Automation")
    grants = preflight(ctx)
    push_target = env.flag(ctx, "ENABLE_ZFS_PUSH_TARGET")

    log.action("TRIM")
    systemd.ensure_running(ctx, "fstrim.timer", changed=False)

    log.action("Sanoid / Syncoid")
    packages.ensure(ctx, *PACKAGES)

    remove_shadow_copy(ctx)
    if push_target:
        prepare_push_user(ctx)
    else:
        remove_push_access(ctx)
    units_retired = retire_obsolete_replication(ctx)

    log.action("Files")
    files.install_all(ctx)

    if push_target:
        grant_push_access(ctx, grants)
    refresh_known_hosts(ctx)

    # The packaged timer would snapshot on sanoid's own schedule, alongside ours.
    systemd.ensure_stopped(ctx, "sanoid.timer")

    units = [name for name in ctx.file_map if name.endswith((".service", ".timer"))]
    if units_retired or ctx.changes.touched(*units):
        systemd.daemon_reload(ctx)

    if env.flag(ctx, "PAUSED"):
        # A host-wide freeze that overrides every ENABLE_ZFS_* flag. Replication
        # timers are taken from disk, not the map, so none can be missed.
        on_disk = sorted(
            path.name for path in Path(SYSTEMD_DIR).glob(f"{REPLICATION_PREFIX}*.timer")
        )
        systemd.pause(ctx, SNAPSHOT_TIMER, SCRUB_TIMER, *on_disk)
        log.header("ZFS Automation paused")
        return

    set_timer(ctx, SNAPSHOT_TIMER, env.flag(ctx, "ENABLE_ZFS_SNAPSHOTS"))

    replication = env.flag(ctx, "ENABLE_ZFS_REPLICATION")
    paused_jobs = set(ctx.env["PAUSED_REPLICATION_TIMERS"].split())
    for timer in replication_timers(ctx):
        set_timer(ctx, timer, replication and timer not in paused_jobs)

    if replication and env.flag(ctx, "ZFS_REPLICATION_RECOVERY_START_FAILED"):
        for timer in replication_timers(ctx):
            if timer not in paused_jobs:
                systemd.recover_failed(ctx, timer.removesuffix(".timer") + ".service")

    set_timer(ctx, SCRUB_TIMER, env.flag(ctx, "ENABLE_ZFS_SCRUB"))


if __name__ == "__main__":
    run(install, "ZFS Automation")
