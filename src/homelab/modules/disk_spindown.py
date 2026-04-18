from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..media_storage import load_media_storage
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-disk-spindown"
TEMPLATE_FILES = ["homelab-disk-spindown.service"]


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
class DiskSpindownConfig:
    idle_seconds: int
    command_type: str
    symlink_policy: int
    device_labels: tuple[str, ...]


FILE_SPECS = (
    FileSpec("homelab-disk-spindown.defaults", "/etc/default/homelab-disk-spindown"),
    FileSpec(
        "homelab-disk-spindown.service",
        "/etc/systemd/system/homelab-disk-spindown.service",
    ),
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="disk-spindown")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping disk-spindown (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "disk-spindown" / "templates"
    installer = root / "disk-spindown" / "scripts" / "install.sh"

    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        normalize_config(registry, host)


def normalize_config(registry, host: str) -> DiskSpindownConfig:
    media_storage = load_media_storage(registry, host)
    if media_storage is None:
        raise ValueError(f"media_storage is required for disk-spindown on {host}")

    idle_seconds = int(registry.get(host, "disk-spindown.idle_seconds", 1800))
    if idle_seconds < 300:
        raise ValueError(f"disk-spindown.idle_seconds must be at least 300 for {host}")

    command_type = str(registry.get(host, "disk-spindown.command_type", "ata")).strip().lower()
    if command_type not in {"ata", "scsi"}:
        raise ValueError(f"disk-spindown.command_type must be ata or scsi for {host}")

    symlink_policy = int(registry.get(host, "disk-spindown.symlink_policy", 1))
    if symlink_policy not in {0, 1}:
        raise ValueError(f"disk-spindown.symlink_policy must be 0 or 1 for {host}")

    include_parity = str(registry.get(host, "disk-spindown.include_parity", "true")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    device_labels = [
        label
        for label, _path in media_storage.raw_mounts()
        if include_parity or not label.startswith("parity")
    ]
    if not device_labels:
        raise ValueError(f"disk-spindown requires at least one target disk for {host}")

    return DiskSpindownConfig(
        idle_seconds=idle_seconds,
        command_type=command_type,
        symlink_policy=symlink_policy,
        device_labels=tuple(device_labels),
    )


def build_hd_idle_opts(config: DiskSpindownConfig) -> str:
    parts = [
        "-i",
        "0",
        "-c",
        config.command_type,
        "-s",
        str(config.symlink_policy),
    ]
    for label in config.device_labels:
        parts.extend(["-a", f"/dev/disk/by-label/{label}", "-i", str(config.idle_seconds)])
    return " ".join(parts)


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    config = normalize_config(default_registry(root), host)
    module_dir = root / "disk-spindown"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        module_dir / "templates" / "homelab-disk-spindown.service",
        build_dir / "homelab-disk-spindown.service",
    )
    write_env_file(
        build_dir / "homelab-disk-spindown.defaults",
        {"HD_IDLE_OPTS": build_hd_idle_opts(config)},
    )
    write_file_map(build_dir)
    return HostArtifacts(build_dir=build_dir, file_specs=FILE_SPECS)


def write_file_map(build_dir: Path) -> None:
    lines = [f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in FILE_SPECS]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    artifacts = build_host_artifacts(root, host)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.ssh_config.user", registry.get(host, "config.user")))
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    print_sub("Comparing with remote configs...")
    diffs = [
        (artifacts.build_dir / spec.build_name, spec.remote_path)
        for spec in artifacts.file_specs
    ]
    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "disk-spindown" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
