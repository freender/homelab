"""guest-disks: format and mount a VM's `pve-guest` role disks inside the guest.

The disks are described once, in the guest's `pve-guest` block (homelab-ops#154:
a second block would describe each disk twice); `guest-disks: true` only opts a
VM in. Every disk with a mount path (`appdata`, `scratch`, `data`) becomes one
line of `disks.conf`; the installer owns every decision that needs live state --
whether to mkfs, whether fstab or a mount is in the way -- and a dry run stages
it with `PLAN_ONLY=true` so it prints that plan without writing anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, run_module_deploy
from ..op_secrets import offline_mode
from ..output import print_sub
from .pve_guest.inventory import pve_nodes
from .pve_guest.spec import FEATURE as PVE_GUEST
from .pve_guest.spec import GuestSpec, parse_guest

FEATURE = "guest-disks"
REMOTE_ROOT = "/tmp/homelab-guest-disks"
INSTALLER = "scripts/install.py"
DISKS_FILE = "disks.conf"
# The fleet's Debian-family type (helm, riven); zion is `haos` and cannot run this.
SUPPORTED_TYPE = "ubuntu"
# fstrim.timer trims every guest weekly, so no `discard` mount option.
BASE_OPTIONS = "defaults,noatime"
# Only scratch may be missing at boot: its stacks regenerate into an empty dir.
# A missing appdata or data disk must stop the boot rather than let services
# start fresh state on the OS disk (homelab-ops#156).
NOFAIL_ROLES = frozenset({"scratch"})
# The installer finds a disk by its virtio-scsi by-id name, which only scsiN has.
SCSI_KEY_RE = re.compile(r"^scsi\d+$")


@dataclass(frozen=True)
class DiskMount:
    key: str
    role: str
    mount: str
    options: str


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        FEATURE,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda supported_hosts, _hosts: validate(root, supported_hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    script = root / FEATURE / INSTALLER
    if not script.is_file():
        raise ValueError(f"missing installer: {script}")
    for host in hosts:
        disk_mounts(root, host)


def disk_mounts(root: Path, host: str) -> tuple[DiskMount, ...]:
    registry = default_registry(root)
    host_type = str(registry.get(host, "config.type"))
    if host_type != SUPPORTED_TYPE:
        raise ValueError(f"{host}: {FEATURE} only supports {SUPPORTED_TYPE} hosts")
    raw = registry.get(host, PVE_GUEST, None)
    if raw is None:
        raise ValueError(f"{host}: {FEATURE} reads its disks from a {PVE_GUEST} block; add one")
    return plan_mounts(parse_guest(host, raw, pve_nodes(registry)))


def plan_mounts(spec: GuestSpec) -> tuple[DiskMount, ...]:
    """Every disk the guest mounts, parents before children."""
    if spec.kind != "qemu":
        raise ValueError(f"{spec.host}: {FEATURE} is for VMs; PVE mounts an lxc's volumes itself")
    mounts = []
    for disk in spec.disks:
        if disk.mount is None:
            continue
        if not SCSI_KEY_RE.match(disk.key):
            raise ValueError(f"{spec.host}: {FEATURE} only mounts scsiN disks, not {disk.key}")
        mounts.append(DiskMount(disk.key, disk.role, disk.mount, mount_options(disk.role)))
    if not mounts:
        raise ValueError(f"{spec.host}: {FEATURE} is set but no {PVE_GUEST} disk has a mount")
    return tuple(sorted(mounts, key=lambda item: item.mount))


def mount_options(role: str) -> str:
    return f"{BASE_OPTIONS},nofail" if role in NOFAIL_ROLES else BASE_OPTIONS


def render_disks_conf(mounts: tuple[DiskMount, ...]) -> str:
    return "".join(f"{m.key}|{m.role}|{m.mount}|{m.options}\n" for m in mounts)


def build_host(root: Path, host: str) -> Path:
    mounts = disk_mounts(root, host)
    build_dir = root / FEATURE / "build" / host
    prepare_build_dir(build_dir)
    (build_dir / DISKS_FILE).write_bytes(render_disks_conf(mounts).encode())
    for mount in mounts:
        print_sub(escape(f"{mount.key} ({mount.role}) -> {mount.mount} [{mount.options}]"))
    return build_dir


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = build_host(root, host)
    if dry_run and offline_mode():
        print_sub("[?] live disk plan skipped (offline validation)")
        return
    env = force_env(force) | {"PLAN_ONLY": "true" if dry_run else "false"}
    stage_and_run_remote_installer(
        root,
        connection_for_host(root, host),
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / FEATURE / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=env,
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "src"),
    )
