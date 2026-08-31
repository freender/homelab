from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    FileSpec,
    HostArtifacts,
    feature_paused,
    require_text,
    run_module_deploy,
    write_file_map,
)
from ..output import print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-disk-spindown"
TEMPLATE_FILES = [
    "homelab-disk-spindown.service",
    "homelab-disk-wakeup",
    "homelab-disk-wakeup.service",
    "homelab-disk-wakeup.timer",
]


@dataclass(frozen=True)
class DiskSpindownConfig:
    retire: bool
    paused: bool
    idle_seconds: int
    command_type: str
    symlink_policy: int
    wakeup_schedule: str


FILE_SPECS = (
    FileSpec("homelab-disk-spindown.defaults", "/etc/default/homelab-disk-spindown"),
    FileSpec(
        "homelab-disk-spindown.service",
        "/etc/systemd/system/homelab-disk-spindown.service",
    ),
    FileSpec("homelab-disk-wakeup", "/usr/local/sbin/homelab-disk-wakeup", "755"),
    FileSpec(
        "homelab-disk-wakeup.service",
        "/etc/systemd/system/homelab-disk-wakeup.service",
    ),
    FileSpec(
        "homelab-disk-wakeup.timer",
        "/etc/systemd/system/homelab-disk-wakeup.timer",
    ),
)


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
        "disk-spindown",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


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
    host_type = str(registry.get(host, "config.type"))
    if host_type != "pve":
        raise ValueError(f"disk-spindown supports PVE hosts only: {host}")

    retire = registry.get(host, "disk-spindown.retire", False)
    if not isinstance(retire, bool):
        raise ValueError(f"disk-spindown.retire must be boolean for {host}")

    # NOTE: `disk-spindown.paused` is a separate knob from the host-level
    # `deploy: false` targeting gate (see hosts.conf header comment). Setting
    # `deploy: false` removes the host from deploy targets entirely and never
    # touches the running service. `paused: true` keeps the module deploying so
    # it can actively stop/disable the hd-idle service and wakeup timer on the
    # host, and can be flipped back to resume spin-down.
    paused = feature_paused(registry, host, "disk-spindown")

    idle_seconds = int(registry.get(host, "disk-spindown.idle_seconds", 1800))
    if idle_seconds < 300:
        raise ValueError(f"disk-spindown.idle_seconds must be at least 300 for {host}")

    command_type = str(registry.get(host, "disk-spindown.command_type", "ata")).strip().lower()
    if command_type not in {"ata", "scsi"}:
        raise ValueError(f"disk-spindown.command_type must be ata or scsi for {host}")

    symlink_policy = int(registry.get(host, "disk-spindown.symlink_policy", 1))
    if symlink_policy not in {0, 1}:
        raise ValueError(f"disk-spindown.symlink_policy must be 0 or 1 for {host}")

    wakeup_schedule = require_text(
        registry.get(host, "disk-spindown.wakeup_schedule", "*-*-* 03:50:00"),
        f"disk-spindown.wakeup_schedule must be non-empty for {host}",
    )

    return DiskSpindownConfig(
        retire=retire,
        paused=paused,
        idle_seconds=idle_seconds,
        command_type=command_type,
        symlink_policy=symlink_policy,
        wakeup_schedule=wakeup_schedule,
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    config = normalize_config(default_registry(root), host)
    module_dir = root / "disk-spindown"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        module_dir / "templates" / "homelab-disk-spindown.service",
        build_dir / "homelab-disk-spindown.service",
    )
    for file_name in (
        "homelab-disk-wakeup",
        "homelab-disk-wakeup.service",
        "homelab-disk-wakeup.timer",
    ):
        render_file(
            module_dir / "templates" / file_name,
            build_dir / file_name,
            WAKEUP_SCHEDULE=config.wakeup_schedule,
        )
    write_env_file(
        build_dir / "homelab-disk-spindown.defaults",
        {
            "HD_IDLE_ENABLED": "false" if config.paused else "true",
            "HD_IDLE_IDLE_SECONDS": config.idle_seconds,
            "HD_IDLE_COMMAND_TYPE": config.command_type,
            "HD_IDLE_SYMLINK_POLICY": config.symlink_policy,
        },
    )
    write_file_map(build_dir, FILE_SPECS)
    return HostArtifacts(build_dir=build_dir, file_specs=FILE_SPECS)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    config = normalize_config(registry, host)
    if config.retire:
        print_sub(f"disk-spindown retired for {host}; will remove hd-idle and managed files")
    if config.paused:
        print_sub(f"disk-spindown paused for {host}; will stop hd-idle on deploy")
    artifacts = build_host_artifacts(root, host)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    print_sub("Comparing with remote configs...")
    diff_pairs = [
        (artifacts.build_dir / spec.build_name, spec.remote_path)
        for spec in artifacts.file_specs
    ]
    for message in diff_many(connection, diff_pairs):
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
        env={
            **force_env(force),
            "RETIRE_DISK_SPINDOWN": "true" if config.retire else "false",
        },
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
