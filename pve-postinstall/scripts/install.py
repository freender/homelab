#!/usr/bin/env python3
"""Remote installer for the pve-postinstall module (freender/homelab-ops#30).

Post-install convergence for the PVE nodes: the no-subscription and pve-test repo
sources, the nag removal hook, the sshd hardening drop-in, the cluster rejoin helper,
the timezone, ZFS pool import and local zfspool storage, native scrub timers, the
`openipmi` mask, `/etc/network/interfaces`, declared disk mounts, local postfix, and a
report of the manual `pvecm add` step on a node that should be clustered and is not.

Replaces `install.sh` and the two sub-installers it shelled out to,
`install-interfaces.sh` and `install-mounts.sh`.

Behaviour changes from `install.sh`:

* **The installer's values move to `build/<host>/env`, parsed with every key
  required.** The bash took seven positional arguments and defaulted each one, so a
  short argument list read as `UTC`, no pools, no mounts and *not clustered*. The
  host type is no longer passed at all: the orchestrator already refuses anything
  but `pve`, and the installer checks for `pveversion` instead.
* **Every refusal comes before any write.** A missing build file, an unknown
  timezone, a malformed mount, or pools to import on a host with no `zpool` used to
  surface after the apt sources, the timezone, or both had already been changed.
* **The timezone is only set when it differs.** The bash ran `timedatectl
  set-timezone`, relinked `/etc/localtime` and rewrote `/etc/timezone` on every
  deploy. An unknown timezone now refuses instead of warning.
* **A changed sshd drop-in that `sshd -t` rejects is rolled back through
  `files.install_validated`,** as the bash did by hand, and **a failed reload fails the
  deploy.** The bash's `systemctl reload sshd && print_sub` sat in an `&&` list, which
  `set -e` ignores, so a reload failure was silent.
* **The widget toolkit is reinstalled right after the nag files change,** not as the
  last step. A later failure used to skip it, and the next deploy saw no change and
  never retried.
* **postfix is neither reloaded nor re-aliased on every deploy.** `newaliases` runs
  only when `/etc/aliases` is newer than its database, and postfix re-reads a changed
  hash map by itself; nothing else in this repo edits postfix. A postfix that cannot
  be enabled or started fails the deploy (it was a warning).
* **Mounts are matched by fstab field, not substring.** `grep -q /mnt/media` also
  matched `/mnt/media2` and commented-out lines, so a new mount could be skipped as
  "already present". A failed `mount -a` still fails the deploy.
* **The peer certificate fingerprint is read with `ssl`,** not an `openssl s_client`
  pipeline, and each rejoin peer is tried once (the preferred peer was tried twice).

Kept as warnings, deliberately: a pool that will not import and a `pvesm` call that
fails. Both matter most on a freshly rebuilt node, where the interfaces, mounts and
cluster-join report that follow them are the parts worth getting through.
"""

from __future__ import annotations

import difflib
import filecmp
import hashlib
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from homelab_install import env, files, log, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which

REPO_SOURCES = ("proxmox.sources", "pve-test.sources")
NAG_FILES = ("pve-remove-nag.sh", "no-nag-script")
SSHD_DROP_IN = "sshd-hardening.conf"
REJOIN_HELPER = "homelab-pve-cluster-rejoin-helper"
REQUIRED_FILES = (*REPO_SOURCES, *NAG_FILES, SSHD_DROP_IN, REJOIN_HELPER)

SOURCES_DIR = "/etc/apt/sources.list.d"
NO_NAG_DEST = "/etc/apt/apt.conf.d/no-nag-script"
BACKUP_DIR = "/var/backups/homelab/pve-postinstall"

# PVE installs these pointing at the enterprise repos, and without a subscription
# every `apt-get update` fails 401 and breaks every apt-driven module. PVE 9 added the
# deb822 `ceph.sources`; no node runs Ceph, so it is dropped rather than repointed.
ENTERPRISE_SOURCES = (
    "/etc/apt/sources.list.d/pve-enterprise.sources",
    "/etc/apt/sources.list.d/ceph.list",
    "/etc/apt/sources.list.d/ceph-enterprise.list",
    "/etc/apt/sources.list.d/ceph.sources",
)

ZONEINFO_DIR = "/usr/share/zoneinfo"
LOCALTIME = "/etc/localtime"
TIMEZONE_FILE = "/etc/timezone"

STORAGE_CFG = "/etc/pve/storage.cfg"
COROSYNC_CONF = "/etc/pve/corosync.conf"
LOCAL_STORAGE_POOLS = ("vm-disks", "vm-flash", "vault-disks", "vault-hdd")

INTERFACES = "/etc/network/interfaces"
FSTAB = "/etc/fstab"
MOUNT_OPTIONS = "nosuid,nodev,nofail,x-systemd.device-timeout=60,x-systemd.mount-timeout=60"

ALIASES = "/etc/aliases"
ALIASES_DB = "/etc/aliases.db"

DOMAIN = "freender.internal"
CLUSTER_PEERS = tuple(f"{node}.{DOMAIN}" for node in ("ace", "bray", "clovis"))
REJOIN_HELPER_PATH = "/usr/local/sbin/homelab-pve-cluster-rejoin-helper"


@dataclass(frozen=True)
class Settings:
    timezone: str
    import_pools: tuple[str, ...]
    mounts: tuple[tuple[str, str], ...]
    expected_clustered: bool
    cluster_link0: str


def read_settings(ctx: InstallContext) -> Settings:
    """Parse the env file. The three list-ish keys may be empty but must be present:
    empty is "none configured", absent is a truncated render."""
    env.require(ctx, "TIMEZONE", "EXPECTED_CLUSTERED")
    absent = [name for name in ("IMPORT_POOLS", "MOUNTS", "CLUSTER_LINK0") if name not in ctx.env]
    if absent:
        raise InstallError(
            f"incomplete env file at {ctx.build_dir / 'env'} (missing: {', '.join(absent)}); "
            "refusing to run with an ambiguous config"
        )

    mounts: list[tuple[str, str]] = []
    for entry in ctx.env["MOUNTS"].split():
        label, _sep, target = entry.partition(":")
        if not label or not target.startswith("/"):
            raise InstallError(f"malformed mount {entry!r}; expected label:/absolute/path")
        mounts.append((label, target))

    return Settings(
        timezone=ctx.env["TIMEZONE"].strip(),
        import_pools=tuple(ctx.env["IMPORT_POOLS"].split()),
        mounts=tuple(mounts),
        expected_clustered=env.flag(ctx, "EXPECTED_CLUSTERED"),
        cluster_link0=ctx.env["CLUSTER_LINK0"].strip(),
    )


def preflight(ctx: InstallContext, settings: Settings) -> None:
    if not _which("pveversion"):
        raise InstallError("pveversion not found; pve-postinstall only runs on a PVE host")

    missing = [name for name in REQUIRED_FILES if not (ctx.build_dir / name).is_file()]
    unmapped = [name for name in REQUIRED_FILES if name not in ctx.file_map]
    if missing:
        raise InstallError(f"missing in {ctx.build_dir}: {', '.join(missing)}")
    if unmapped:
        raise InstallError(f"missing file-map entries: {', '.join(unmapped)}")

    if not Path(ZONEINFO_DIR, settings.timezone).is_file():
        raise InstallError(f"timezone data not found for {settings.timezone}")

    if settings.import_pools and not _which("zpool"):
        raise InstallError(f"zpool not found; cannot import {' '.join(settings.import_pools)}")


def _differs(src: Path, dest: str) -> bool:
    dest_path = Path(dest)
    return not dest_path.is_file() or not filecmp.cmp(src, dest_path, shallow=False)


def back_up_apt_state(ctx: InstallContext) -> None:
    """Copy aside what the repo and nag installs are about to replace.

    Into `BACKUP_DIR`, not beside the originals: both live in directories apt parses,
    where a sibling `.bak` copy would itself be read as config.
    """
    stamp = time.strftime("%Y%m%d%H%M%S")
    if any(_differs(ctx.build_dir / name, ctx.file_map[name][0]) for name in REPO_SOURCES):
        if Path(SOURCES_DIR).is_dir():
            log.sub(f"Backing up {SOURCES_DIR}...")
            Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
            shutil.copytree(SOURCES_DIR, f"{BACKUP_DIR}/sources.list.d.{stamp}", symlinks=True)
    else:
        log.sub(f"{SOURCES_DIR} unchanged; skipping backup")

    if _differs(ctx.build_dir / "no-nag-script", NO_NAG_DEST):
        if Path(NO_NAG_DEST).is_file():
            log.sub("Backing up no-nag-script...")
            Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
            shutil.copy2(NO_NAG_DEST, f"{BACKUP_DIR}/no-nag-script.{stamp}")
    else:
        log.sub("no-nag-script unchanged; skipping backup")


def ensure_timezone(ctx: InstallContext, timezone: str) -> None:
    current = _run(
        ["timedatectl", "show", "-p", "Timezone", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    if (current.stdout or "").strip() != timezone:
        result = _run(["timedatectl", "set-timezone", timezone], check=False)
        if result.returncode != 0:
            raise InstallError(
                f"timedatectl set-timezone {timezone} failed (exit {result.returncode})"
            )
        log.ok(f"Timezone set to {timezone}")
        ctx.changes.record(TIMEZONE_FILE)

    zoneinfo = f"{ZONEINFO_DIR}/{timezone}"
    localtime = Path(LOCALTIME)
    if not (localtime.is_symlink() and os.readlink(localtime) == zoneinfo):
        if localtime.is_symlink() or localtime.exists():
            localtime.unlink()
        localtime.symlink_to(zoneinfo)
        log.sub(f"Linked {LOCALTIME} to {zoneinfo}")
        ctx.changes.record(LOCALTIME)

    timezone_file = Path(TIMEZONE_FILE)
    if not timezone_file.is_file() or timezone_file.read_text(encoding="utf-8") != f"{timezone}\n":
        timezone_file.write_text(f"{timezone}\n", encoding="utf-8")
        log.sub(f"Wrote {TIMEZONE_FILE}")
        ctx.changes.record(TIMEZONE_FILE)

    if not ctx.changes.touched(TIMEZONE_FILE, LOCALTIME):
        log.sub(f"Timezone already {timezone}")


def refresh_widget_toolkit(ctx: InstallContext) -> None:
    """Reinstall the toolkit so the changed nag hook patches a pristine copy.

    `pve-remove-nag.sh` skips a file that already carries its marker, so a changed
    script does nothing to an already-patched toolkit until it is reinstalled. A
    failure only warns: the hook runs again on the next apt transaction regardless.
    """
    log.sub("Refreshing proxmox widget toolkit...")
    result = _run(
        ["apt-get", "install", "--reinstall", "-y", "-q", "proxmox-widget-toolkit"],
        check=False,
        capture_output=True,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if result.returncode != 0:
        log.warn(
            "Widget toolkit reinstall failed; run manually: "
            "apt --reinstall install proxmox-widget-toolkit"
        )


def install_sshd_drop_in(ctx: InstallContext) -> None:
    if not files.install_validated(ctx, SSHD_DROP_IN, ["sshd", "-t"]):
        return
    result = _run(["systemctl", "reload", "ssh"], check=False)
    if result.returncode != 0:
        raise InstallError(f"systemctl reload ssh failed (exit {result.returncode})")
    log.sub("sshd reloaded with hardened config")


def _pool_imported(pool: str) -> bool:
    return _run(["zpool", "list", pool], check=False, capture_output=True).returncode == 0


def import_pools(ctx: InstallContext, pools: tuple[str, ...]) -> None:
    if not pools:
        log.sub("No ZFS pools configured for import; skipping")
        return
    for pool in pools:
        if _pool_imported(pool):
            log.sub(f"Pool {pool} already imported; skipping")
            continue
        log.sub(f"Importing ZFS pool: {pool}")
        if _run(["zpool", "import", "-f", pool], check=False).returncode != 0:
            log.warn(f"Failed to import pool {pool}")


def storage_nodes(storage: str) -> list[str]:
    """The `nodes` list of a zfspool storage in `storage.cfg`, empty when unrestricted.

    A section runs from its `zfspool: <name>` header to the next line that does not
    start with whitespace, blank lines included -- the same boundary the bash awk used.
    """
    try:
        text = Path(STORAGE_CFG).read_text(encoding="utf-8")
    except OSError:
        return []

    inside = False
    for line in text.splitlines():
        fields = line.split()
        if not line[:1].isspace():
            inside = fields[:2] == ["zfspool:", storage]
            continue
        if inside and len(fields) > 1 and fields[0] == "nodes":
            return fields[1].split(",")
    return []


def ensure_zfspool_storage(ctx: InstallContext, pool: str, node: str) -> None:
    if not _pool_imported(pool):
        return

    if (
        _run(["pvesm", "status", "--storage", pool], check=False, capture_output=True).returncode
        == 0
    ):
        log.sub(f"{pool} storage already configured")
        nodes = storage_nodes(pool)
        if nodes and node not in nodes:
            log.sub(f"Adding {node} to {pool} storage node list")
            command = ["pvesm", "set", pool, "--nodes", ",".join([*nodes, node])]
            if _run(command, check=False).returncode != 0:
                log.warn(f"failed to update {pool} storage nodes")
            else:
                ctx.changes.record(f"storage:{pool}")
        return

    log.sub(f"Creating {pool} storage on {pool}...")
    command = [
        "pvesm",
        "add",
        "zfspool",
        pool,
        "--pool",
        pool,
        "--content",
        "images,rootdir",
        "--sparse",
        "0",
    ]
    if Path(COROSYNC_CONF).is_file():
        command += ["--nodes", node]
    if _run(command, check=False).returncode != 0:
        log.warn(f"failed to create {pool} storage")
    else:
        ctx.changes.record(f"storage:{pool}")


def ensure_local_storage(ctx: InstallContext) -> None:
    for tool in ("pvesm", "zpool"):
        if not _which(tool):
            log.warn(f"{tool} not found; skipping zfs storage reconciliation")
            return
    if not _pool_imported("rpool"):
        log.warn("rpool not found; skipping zfs storage reconciliation")
        return

    node = _short_hostname()
    for pool in LOCAL_STORAGE_POOLS:
        ensure_zfspool_storage(ctx, pool, node)


def configure_scrub_timers(ctx: InstallContext) -> None:
    """Scrub every pool monthly through PVE's native per-pool timers.

    Replaces the single `zfs-scrub.timer` and any weekly instance, so a pool is never
    scrubbed on two schedules.
    """
    if not _which("zpool"):
        log.warn("zpool not found; skipping native scrub timer setup")
        return
    if _run(
        ["systemctl", "cat", "zfs-scrub-monthly@.timer"], check=False, capture_output=True
    ).returncode:
        log.warn("native zfs-scrub-monthly@.timer not available; skipping scrub timer setup")
        return

    systemd.ensure_stopped(ctx, "zfs-scrub.timer")
    pools = _run(["zpool", "list", "-H", "-o", "name"], check=True, capture_output=True, text=True)
    for pool in pools.stdout.split():
        systemd.ensure_stopped(ctx, f"zfs-scrub-weekly@{pool}.timer")
        systemd.ensure_running(ctx, f"zfs-scrub-monthly@{pool}.timer", changed=False)


def install_interfaces(ctx: InstallContext) -> None:
    """Write `/etc/network/interfaces` without applying it.

    ifupdown2 reads the file only on `ifreload -a` or at boot, so a write leaves the
    running network untouched. That is deliberate: reloading can drop the management
    path, and nothing here could recover a host it just cut off. The warning is loud
    because a silent write leaves the host diverged until some unrelated reboot
    applies the change by surprise.

    `mode=None`: the bash `cp` kept the existing file's mode.
    """
    staged = ctx.build_dir / "interfaces"
    if not staged.is_file():
        log.sub("Network interfaces not configured; skipping")
        return

    dest = Path(INTERFACES)
    before = dest.read_text(encoding="utf-8").splitlines() if dest.is_file() else None
    if not files.install_from(ctx, staged, INTERFACES, None, backup=True, record="interfaces"):
        return

    log.warn(f"{INTERFACES} changed but is NOT live; running network is unchanged")
    if before is not None:
        after = staged.read_text(encoding="utf-8").splitlines()
        changed = [
            line
            for line in difflib.unified_diff(before, after, lineterm="")
            if len(line) > 1 and line[0] in "+-" and line[1] not in "+-"
        ]
        if changed:
            log.sub("Pending change:")
            for line in changed:
                log.sub(f"    {line}")
    log.sub("Apply with: ifquery --check -a   then   ifreload -a   (or reboot)")


def configure_mounts(ctx: InstallContext, mounts: tuple[tuple[str, str], ...]) -> None:
    if not mounts:
        log.sub("No mounts configured; skipping")
        return

    fstab = Path(FSTAB)
    text = fstab.read_text(encoding="utf-8") if fstab.is_file() else ""
    entries = [
        line.split()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    for label, target in mounts:
        log.action(f"Configuring mount for LABEL={label} at {target}")
        Path(target).mkdir(parents=True, exist_ok=True)
        if any(fields[0] == f"LABEL={label}" or fields[1:2] == [target] for fields in entries):
            log.sub(f"LABEL={label} already in {FSTAB}; skipping")
            continue
        line = f"LABEL={label} {target} auto {MOUNT_OPTIONS} 0 2"
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{line}\n"
        fstab.write_text(text, encoding="utf-8")
        entries.append(line.split())
        ctx.changes.record(FSTAB)
        log.ok(f"Added LABEL={label} to {FSTAB}")

    log.action("Mounting all filesystems...")
    systemd.daemon_reload(ctx)
    result = _run(["mount", "-a"], check=False)
    if result.returncode != 0:
        raise InstallError(f"mount -a failed (exit {result.returncode})")
    log.ok("Mounts complete")


def configure_postfix(ctx: InstallContext) -> None:
    if _run(
        ["systemctl", "list-unit-files", "postfix.service"], check=False, capture_output=True
    ).returncode:
        log.sub("postfix service not present; skipping")
        return

    aliases = Path(ALIASES)
    database = Path(ALIASES_DB)
    stale = aliases.is_file() and (
        not database.is_file() or aliases.stat().st_mtime > database.stat().st_mtime
    )
    if stale and _which("newaliases"):
        if _run(["newaliases"], check=False).returncode != 0:
            log.warn("failed to rebuild postfix aliases")
        else:
            log.ok("postfix aliases rebuilt")

    systemd.ensure_running(ctx, "postfix.service", changed=False)


def _short_hostname() -> str:
    return socket.gethostname().split(".", 1)[0]


def _fetch_certificate(peer: str) -> str:
    return ssl.get_server_certificate((peer, 8006), timeout=10)


def peer_fingerprint(peer: str) -> str:
    """SHA-256 fingerprint of the peer's PVE API certificate, as `pvecm add` takes it."""
    try:
        der = ssl.PEM_cert_to_DER_cert(_fetch_certificate(peer))
    except (OSError, ValueError):
        return ""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def print_manual_join(settings: Settings, peer: str) -> None:
    if Path(COROSYNC_CONF).is_file():
        log.sub("Cluster config appeared after cleanup; manual pvecm add not needed")
        return

    link0 = settings.cluster_link0
    if not link0:
        addresses = _run(["hostname", "-I"], check=False, capture_output=True, text=True)
        link0 = next(iter((addresses.stdout or "").split()), "")

    fingerprint = peer_fingerprint(peer)
    log.warn("Cluster join is a manual step")
    if fingerprint:
        log.sub(f"Manual command: pvecm add {peer} --fingerprint {fingerprint} --link0 {link0}")
    else:
        log.warn("Cluster peer fingerprint unavailable; verify it manually")
        log.sub(f"Manual command: pvecm add {peer} --link0 {link0}")


def report_cluster_join(ctx: InstallContext, settings: Settings) -> None:
    """On a node that should be clustered and is not, clean up from a peer and print
    the join command. Never joins: `pvecm add` stays a manual step."""
    if not settings.expected_clustered:
        return
    if (
        Path(COROSYNC_CONF).is_file()
        and _run(["pvecm", "status"], check=False, capture_output=True).returncode == 0
    ):
        log.sub("Cluster membership detected")
        return

    node = _short_hostname()
    hint = f"bray.{DOMAIN}" if node == "ace" else f"ace.{DOMAIN}"
    log.warn(f"{node} is expected to be clustered, but currently appears standalone")
    log.sub(f"Attempting safe cleanup/readiness from cluster peer {hint}")

    for peer in dict.fromkeys((hint, *CLUSTER_PEERS)):
        if peer == f"{node}.{DOMAIN}":
            continue
        remote = (
            f"test -x {REJOIN_HELPER_PATH} && "
            f"{REJOIN_HELPER_PATH} {shlex.quote(node)} {shlex.quote(peer)}"
        )
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"root@{peer}", remote]
        if _run(command, check=False).returncode == 0:
            print_manual_join(settings, peer)
            return

    log.warn("Automatic cluster cleanup failed; run from an existing cluster node:")
    log.sub(f"homelab-pve-cluster-rejoin-helper {node} {hint}")
    print_manual_join(settings, hint)


def install(ctx: InstallContext) -> None:
    log.header("PVE Post-Install")

    settings = read_settings(ctx)
    preflight(ctx, settings)

    log.action("Checking if repo configs need backup")
    back_up_apt_state(ctx)

    log.action("Removing enterprise repository definitions")
    for source in ENTERPRISE_SOURCES:
        files.remove(ctx, source, "enterprise repo")

    log.action(f"Setting timezone to {settings.timezone}")
    ensure_timezone(ctx, settings.timezone)

    log.action("Deploying PVE repo sources")
    for name in REPO_SOURCES:
        files.install(ctx, name)

    log.action("Deploying nag removal")
    nag_changed = [files.install(ctx, name) for name in NAG_FILES]
    if any(nag_changed):
        refresh_widget_toolkit(ctx)
    else:
        log.sub("Nag files unchanged; skipping widget toolkit reinstall")

    log.action("Deploying sshd hardening config")
    install_sshd_drop_in(ctx)

    log.action("Deploying cluster rejoin helper")
    files.install(ctx, REJOIN_HELPER)

    log.action("Importing ZFS pools")
    import_pools(ctx, settings.import_pools)

    log.action("Reconciling local ZFS storage")
    ensure_local_storage(ctx)

    log.action("Configuring native ZFS scrub timers")
    configure_scrub_timers(ctx)

    log.action("Masking unwanted default services")
    # openipmi is an LSB script that fails at boot without a BMC (/dev/ipmi0), and no
    # node here has one. Masked so it never sits in `systemctl --failed` for alerting.
    systemd.mask(ctx, "openipmi.service", "no IPMI hardware on this host")

    log.action("Configuring network interfaces")
    install_interfaces(ctx)

    log.action("Configuring disk mounts")
    configure_mounts(ctx, settings.mounts)

    report_cluster_join(ctx, settings)

    log.action("Configuring local postfix service")
    configure_postfix(ctx)


if __name__ == "__main__":
    run(install, "PVE Post-Install")
