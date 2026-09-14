#!/usr/bin/env python3
"""Remote installer for the pbs-client-backup module (freender/homelab-ops#30).

Installs the PBS client backup script, its config, the per-destination credential
env files, the client-side encryption keyfile, and the
`homelab-pbs-client-backup` service and timer. On Ubuntu it also installs
`proxmox-backup-client` from the public Proxmox pbs-client repo. PVE nodes get the
client from the PVE repo `pve-postinstall` manages, so there it is only checked.

Behaviour changes from `install.sh`:

* **Every refusal comes before any write.** That covers the env file, the host type,
  the destination count, every staged credential and build file, the staged
  keyfile, the Ubuntu suite and its vendored keyring, the PVE client, and `zfs`.
  The bash installed the destination credentials first and only then found out the
  host type was unsupported or `zfs` was missing.
* **An incomplete env file fails the deploy.** The bash `source`d the rendered conf
  and read every flag with a default. So a truncated file read as `ENCRYPT=false`
  with `PURGE_KEYFILE` unset: the keyfile was neither installed nor purged, and the
  deploy reported success. `PAUSED` unset read as "not paused" and re-enabled a
  timer someone had stopped on purpose. The installer's values now arrive in
  `build/<host>/env`, which is parsed, and every key is required.
* **A typo in a flag fails the deploy** (`env.flag`) instead of reading as false.
* **Converged credentials are not rewritten.** The bash `install`ed every
  destination env file on every deploy.
* **The package is checked through dpkg** (`packages.ensure`), not `command -v`,
  and the binary is still verified afterwards. The bash refreshed only the
  pbs-client source; this refreshes all of apt, and only when something is
  actually missing.
* **A changed timer is restarted** through `systemd.ensure_running`. The bash ran
  `enable --now`, which does nothing to a timer that is already running.

**The `pve-config-backup` retirement is not ported.** Both units and all six
files were checked and found absent on `ace` and `osiris`, the only PVE hosts with
this feature. The orchestrator still writes `RETIRE_PVE_CONFIG_BACKUP` into the
runtime conf. Nothing reads it, and it is left there so this port does not rewrite
that file on every host.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which

SERVICE = "homelab-pbs-client-backup.service"
TIMER = "homelab-pbs-client-backup.timer"
MANAGED = (
    "homelab-pbs-client-backup",
    "homelab-pbs-client-backup.conf",
    SERVICE,
    TIMER,
)
REQUIRED_ENV = (
    "HOST_TYPE",
    "DESTINATION_COUNT",
    "NEEDS_ZFS",
    "ENCRYPT",
    "PURGE_KEYFILE",
    "KEYFILE",
    "PAUSED",
)

CLIENT = "proxmox-backup-client"
DESTINATION_ENV = "/etc/homelab/pbs-client-backup-destination-{index}.env"
STAGED_KEYFILE = "pbs-encryption.key"

OS_RELEASE = "/etc/os-release"
KEYRING_DIR = "/usr/share/keyrings"
PBS_CLIENT_SOURCE = "/etc/apt/sources.list.d/pbs-client.sources"
# download.proxmox.com/debian/pbs-client publishes Debian suites only. The packages
# are ABI-compatible with the Ubuntu release each one is mapped to here.
SUITE_BY_VERSION = {"26.04": "trixie", "24.04": "bookworm"}
SUITE_BY_CODENAME = {"resolute": "trixie", "noble": "bookworm"}


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path(OS_RELEASE)
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip():
            tokens = shlex.split(raw, comments=True)
            values[key.strip()] = tokens[0] if tokens else ""
    return values


def ubuntu_suite() -> str:
    """The Proxmox pbs-client suite for this Ubuntu release."""
    release = _os_release()
    suite = SUITE_BY_VERSION.get(release.get("VERSION_ID", "")) or SUITE_BY_CODENAME.get(
        release.get("VERSION_CODENAME", "")
    )
    if not suite:
        raise InstallError(
            "unable to map this Ubuntu release "
            f"({release.get('VERSION_ID', '?')} {release.get('VERSION_CODENAME', '?')}) "
            "to a Proxmox pbs-client suite"
        )
    return suite


def destination_count(ctx: InstallContext) -> int:
    raw = ctx.env["DESTINATION_COUNT"].strip()
    if not raw.isdigit() or int(raw) < 1:
        raise InstallError(f"DESTINATION_COUNT must be a positive integer, got {raw!r}")
    return int(raw)


def preflight(ctx: InstallContext) -> tuple[int, str | None]:
    """Every check that can refuse the deploy, before anything is written.

    Returns the destination count and the Ubuntu suite (None on PVE).
    """
    env.require(ctx, *REQUIRED_ENV)
    host_type = ctx.env["HOST_TYPE"].strip()
    if host_type not in {"ubuntu", "pve"}:
        raise InstallError(f"unsupported HOST_TYPE {host_type!r} (expected 'ubuntu' or 'pve')")
    count = destination_count(ctx)
    # Read now so a typo refuses here rather than halfway through the writes.
    encrypt = env.flag(ctx, "ENCRYPT")
    env.flag(ctx, "PURGE_KEYFILE")
    env.flag(ctx, "PAUSED")

    staged = [ctx.build_dir / name for name in MANAGED]
    staged += [ctx.build_dir / f"destination-{index}.env" for index in range(count)]
    if encrypt:
        staged.append(ctx.build_dir / STAGED_KEYFILE)
    missing = [str(path) for path in staged if not path.is_file()]
    if missing:
        hint = f"; run ./deploy pbs-client-backup {ctx.host} from riven" if encrypt else ""
        raise InstallError(f"missing staged files: {', '.join(missing)}{hint}")
    unmapped = [name for name in MANAGED if name not in ctx.file_map]
    if unmapped:
        raise InstallError(f"missing file-map entries: {', '.join(unmapped)}")

    suite = None
    if host_type == "ubuntu":
        suite = ubuntu_suite()
        keyring = ctx.script_dir / "configs" / "keyrings" / f"proxmox-release-{suite}.gpg"
        if not keyring.is_file():
            raise InstallError(f"missing vendored keyring: {keyring}")
    elif not _which(CLIENT):
        raise InstallError(f"{CLIENT} not found (expected from the PVE repo)")

    if env.flag(ctx, "NEEDS_ZFS") and not _which("zfs"):
        raise InstallError("an archive snapshots a ZFS dataset, but the zfs command is not found")

    return count, suite


def ensure_client_ubuntu(ctx: InstallContext, suite: str) -> None:
    """Install `proxmox-backup-client` from the no-subscription pbs-client repo."""
    keyring = f"{KEYRING_DIR}/proxmox-release-{suite}.gpg"
    source = (
        "Types: deb\n"
        "URIs: http://download.proxmox.com/debian/pbs-client\n"
        f"Suites: {suite}\n"
        "Components: main\n"
        f"Signed-By: {keyring}\n"
    )
    vendored = ctx.script_dir / "configs" / "keyrings" / f"proxmox-release-{suite}.gpg"
    repo_changed = files.install_from(ctx, vendored, keyring, "644")

    source_path = Path(PBS_CLIENT_SOURCE)
    if not source_path.is_file() or source_path.read_text(encoding="utf-8") != source:
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(source, encoding="utf-8")
        source_path.chmod(0o644)
        log.sub(f"Wrote {PBS_CLIENT_SOURCE} (suite: {suite})")
        repo_changed = True
    if repo_changed:
        packages.sources_changed(ctx)

    packages.ensure(ctx, CLIENT)
    if not _which(CLIENT):
        raise InstallError(f"{CLIENT} not found after install")


def report_client_version() -> None:
    result = _run([CLIENT, "version"], check=False, capture_output=True, text=True)
    first = (result.stdout or "").strip().splitlines()[:1]
    log.ok(f"{CLIENT} present{f' ({first[0]})' if first else ''}")


def install_keyfile(ctx: InstallContext) -> None:
    """Install the client-side encryption key, or remove it where nothing reads it.

    The orchestrator clears `PURGE_KEYFILE` on PVE hosts with encrypted vzdump
    storage: guest and `/etc/pve` restores read the same path.
    """
    keyfile = ctx.env["KEYFILE"].strip()
    if env.flag(ctx, "ENCRYPT"):
        files.install_from(ctx, ctx.build_dir / STAGED_KEYFILE, keyfile, "600", record=keyfile)
    elif env.flag(ctx, "PURGE_KEYFILE"):
        files.remove(ctx, keyfile, "encryption disabled")


def install(ctx: InstallContext) -> None:
    log.header("PBS Client Backup")
    count, suite = preflight(ctx)

    if suite:
        ensure_client_ubuntu(ctx, suite)
    report_client_version()

    for index in range(count):
        files.install_from(
            ctx,
            ctx.build_dir / f"destination-{index}.env",
            DESTINATION_ENV.format(index=index),
            "600",
        )
    install_keyfile(ctx)

    # The port of `homelab_reload_and_clear_failed`: clear a failed record only when
    # the definition changed, never start a backup from a deploy.
    if files.install_all(ctx):
        systemd.daemon_reload(ctx)
        systemd.reset_failed(ctx, SERVICE)

    if env.flag(ctx, "PAUSED"):
        systemd.pause(ctx, TIMER)
        log.header("PBS Client Backup paused")
        return

    systemd.ensure_running(ctx, TIMER, changed=ctx.changes.touched(TIMER))
    _run(["systemctl", "list-timers", TIMER, "--no-pager", "--all"], check=False)


if __name__ == "__main__":
    run(install, "PBS Client Backup")
