"""Shared dataclasses and file-spec constants for zfs-automation.

Pure data: no imports from sibling submodules, so every other submodule in this
package can depend on this one without risk of a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REMOTE_ROOT = "/tmp/homelab-zfs-automation"
STATIC_CONFIG_FILES = ["zfs-scrub.timer"]
TEMPLATE_FILES = [
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
    "homelab-zfs-replication.service",
    "homelab-zfs-replication.timer",
    "homelab-zfs-scrub.sh",
    "zfs-scrub.service",
]


@dataclass(frozen=True)
class FileSpec:
    build_name: str
    remote_path: str
    mode: str = "644"


@dataclass(frozen=True)
class SecretFileSpec:
    build_name: str
    remote_path: str
    secret: str
    mode: str = "600"


@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    file_specs: tuple[FileSpec, ...]
    secret_file_specs: tuple[SecretFileSpec, ...] = ()


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
    require_active_lxc: int | None = None


@dataclass(frozen=True)
class MigratableLxcPlan:
    name: str
    vmid: int
    dataset: str


@dataclass(frozen=True)
class MigratableLxcGroup:
    name: str
    plans: tuple[MigratableLxcPlan, ...]


@dataclass(frozen=True)
class ReplicationPlan:
    target: str
    source: str = ""
    require_active_lxc: int | None = None


@dataclass(frozen=True)
class ReplicationJob:
    name: str
    schedule: str
    plans: tuple[ReplicationPlan, ...]
    syncoid_options: tuple[str, ...]
    delete_target_snapshots: bool
    paused: bool = False


@dataclass(frozen=True)
class ZfsPusher:
    name: str
    from_address: str
    public_key: str


@dataclass(frozen=True)
class ZfsPushTargetAccess:
    enabled: bool
    user: str
    datasets: tuple[str, ...]
    pushers: tuple[ZfsPusher, ...]


@dataclass(frozen=True)
class SourcePrivateKey:
    secret: str
    path: str


@dataclass(frozen=True)
class KnownHostRefresh:
    host: str
    known_hosts: str = "/root/.ssh/known_hosts"
    port: int = 22


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
