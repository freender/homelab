"""Shared dataclasses and file-spec constants for zfs-automation.

Pure data: no imports from sibling submodules, so every other submodule in this
package can depend on this one without risk of a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REMOTE_ROOT = "/tmp/homelab-zfs-automation"
INSTALLER = "scripts/install.py"
STATIC_CONFIG_FILES = ["zfs-scrub.timer"]
TEMPLATE_FILES = [
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
    "homelab-zfs-scrub.sh",
    "zfs-scrub.service",
]

# Guest kind -> (log label, systemd unit that is active only on the node running
# the guest). PVE runs each VM as a transient `<vmid>.scope`.
GUEST_ACTIVE_UNITS = {
    "lxc": ("LXC", "pve-container@{vmid}.service"),
    "qemu": ("VM", "{vmid}.scope"),
}


@dataclass(frozen=True)
class FileSpec:
    build_name: str
    remote_path: str
    mode: str = "644"


@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    file_specs: tuple[FileSpec, ...]


@dataclass(frozen=True)
class SnapshotPlan:
    dataset: str
    hourly: str
    daily: str
    weekly: str
    monthly: str
    yearly: str
    recursive: bool = True
    process_children_only: bool = True
    require_active_guest: int | None = None
    guest_kind: str = "lxc"
    autosnap: bool = True


@dataclass(frozen=True)
class MigratableLxcPlan:
    name: str
    vmid: int
    dataset: str
    kind: str = "lxc"


@dataclass(frozen=True)
class MigratableLxcGroup:
    name: str
    plans: tuple[MigratableLxcPlan, ...]


BASE_FILE_SPECS = (
    FileSpec("sanoid.conf", "/etc/sanoid/sanoid.conf"),
    FileSpec(
        "homelab-zfs-snapshots.service",
        "/etc/systemd/system/homelab-zfs-snapshots.service",
    ),
    FileSpec("homelab-zfs-snapshots.timer", "/etc/systemd/system/homelab-zfs-snapshots.timer"),
    FileSpec("homelab-zfs-snapshots.sh", "/usr/local/bin/homelab-zfs-snapshots", mode="755"),
    FileSpec("homelab-zfs-scrub.sh", "/usr/local/bin/homelab-zfs-scrub", mode="755"),
    FileSpec("zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service"),
    FileSpec("zfs-scrub.timer", "/etc/systemd/system/zfs-scrub.timer"),
)
