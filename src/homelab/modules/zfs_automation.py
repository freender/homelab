from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import copy_files, render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-zfs-automation"
STATIC_CONFIG_FILES = ["zfs-scrub.timer"]
TEMPLATE_FILES = [
    "sanoid.conf",
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
    "homelab-zfs-replication.service",
    "homelab-zfs-replication.timer",
    "zfs-scrub.service",
    "homelab-zfs-health-check.service",
    "homelab-zfs-health-check.timer",
]


@dataclass(frozen=True)
class FileSpec:
    build_name: str
    remote_path: str
    mode: str = "644"


@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    zfs_mountpoint: str
    deploy_user: str
    file_specs: tuple[FileSpec, ...]


FILE_SPECS = (
    FileSpec("sanoid.conf", "/etc/sanoid/sanoid.conf"),
    FileSpec("homelab-zfs-snapshots.service", "/etc/systemd/system/homelab-zfs-snapshots.service"),
    FileSpec("homelab-zfs-snapshots.timer", "/etc/systemd/system/homelab-zfs-snapshots.timer"),
    FileSpec("homelab-zfs-replication.service", "/etc/systemd/system/homelab-zfs-replication.service"),
    FileSpec("homelab-zfs-replication.timer", "/etc/systemd/system/homelab-zfs-replication.timer"),
    FileSpec("zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service"),
    FileSpec("zfs-scrub.timer", "/etc/systemd/system/zfs-scrub.timer"),
    FileSpec("homelab-zfs-health-check.service", "/etc/systemd/system/homelab-zfs-health-check.service"),
    FileSpec("homelab-zfs-health-check.timer", "/etc/systemd/system/homelab-zfs-health-check.timer"),
)


def deploy(root: Path, requested_host: str, dry_run: bool, force: bool, session: DeploySession) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="zfs-automation")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping zfs-automation (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / "zfs-automation"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    for file_name in STATIC_CONFIG_FILES:
        file_path = config_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required config: {file_path}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        replication = registry.get(host, "zfs-automation.replication", {})
        if not isinstance(replication, dict):
            raise ValueError(f"zfs-automation.replication must be a mapping for {host}")
        for key in ("source", "target"):
            try:
                registry.get(host, f"zfs-automation.replication.{key}")
            except HostLookupError as exc:
                raise ValueError(f"zfs-automation.replication.{key} required for {host}") from exc


def excluded_datasets_text(zfs_pool: str, excludes: list[str]) -> str:
    if not excludes:
        return ""
    blocks = []
    for dataset in excludes:
        full_dataset = dataset if dataset.startswith(f"{zfs_pool}/") else f"{zfs_pool}/{dataset}"
        blocks.append(f"[{full_dataset}]\nautosnap = no\nautoprune = no\n")
    return "\n".join(blocks) + "\n"


def resolve_remote_path(spec: FileSpec, artifacts: HostArtifacts) -> str:
    return spec.remote_path.format(zfs_mountpoint=artifacts.zfs_mountpoint)


def write_file_map(build_dir: Path, artifacts: HostArtifacts) -> None:
    lines = [f"{spec.build_name}|{resolve_remote_path(spec, artifacts)}|{spec.mode}" for spec in artifacts.file_specs]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        ssh_hostname = str(registry.get(host, "config.hostname", host))
        ssh_user = str(registry.get(host, "config.user"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    module_dir = root / "zfs-automation"
    artifacts = build_host_artifacts(root, host)
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [(artifacts.build_dir / spec.build_name, resolve_remote_path(spec, artifacts)) for spec in artifacts.file_specs],
    ):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy zfs-automation to {host}")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [(artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"), (module_dir / "scripts", f"{REMOTE_ROOT}/scripts")],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    module_dir = root / "zfs-automation"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    deploy_user = str(registry.get(host, "ubuntu-setup.deploy_user", registry.get(host, "config.user")))
    zfs_pool = str(registry.get(host, "zfs-automation.zfs_pool", registry.get(host, "ubuntu-setup.zfs_pool", "cache")))
    zfs_mountpoint = str(registry.get(host, "zfs-automation.zfs_mountpoint", registry.get(host, "ubuntu-setup.zfs_mountpoint", f"/mnt/{zfs_pool}")))
    snapshot_schedule = str(registry.get(host, "zfs-automation.snapshot_schedule", "*-*-* 04:35:00"))
    replication_schedule = str(registry.get(host, "zfs-automation.replication_schedule", "*-*-* 02:30:00"))
    health_check_schedule = str(registry.get(host, "zfs-automation.health_check_schedule", "hourly"))
    replication_source = str(registry.get(host, "zfs-automation.replication.source"))
    replication_target = str(registry.get(host, "zfs-automation.replication.target"))
    replication_post_hook = str(registry.get(host, "zfs-automation.replication_post_hook", ""))
    excludes = registry.get(host, "zfs-automation.sanoid.exclude", [])
    if not isinstance(excludes, list):
        raise ValueError(f"zfs-automation.sanoid.exclude must be a list for {host}")
    excludes = [str(item) for item in excludes]

    # Auto-exclude replication source and target from sanoid — syncoid owns
    # the snapshot lifecycle for both datasets and sanoid must not interfere.
    pool_prefix = f"{zfs_pool}/"
    for dataset in (replication_source, replication_target):
        relative = dataset[len(pool_prefix):] if dataset.startswith(pool_prefix) else dataset
        if relative not in excludes:
            excludes.append(relative)

    hourly = str(registry.get(host, "zfs-automation.sanoid.hourly", 0))
    daily = str(registry.get(host, "zfs-automation.sanoid.daily", 7))
    weekly = str(registry.get(host, "zfs-automation.sanoid.weekly", 4))
    monthly = str(registry.get(host, "zfs-automation.sanoid.monthly", 3))
    yearly = str(registry.get(host, "zfs-automation.sanoid.yearly", 0))

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
    render_file(
        templates_dir / "sanoid.conf",
        build_dir / "sanoid.conf",
        ZFS_POOL=zfs_pool,
        EXCLUDED_DATASETS=excluded_datasets_text(zfs_pool, excludes),
        HOURLY=hourly,
        DAILY=daily,
        WEEKLY=weekly,
        MONTHLY=monthly,
        YEARLY=yearly,
    )
    render_file(templates_dir / "homelab-zfs-snapshots.service", build_dir / "homelab-zfs-snapshots.service")
    render_file(templates_dir / "homelab-zfs-snapshots.timer", build_dir / "homelab-zfs-snapshots.timer", SNAPSHOT_SCHEDULE=snapshot_schedule)
    render_file(templates_dir / "zfs-scrub.service", build_dir / "zfs-scrub.service", ZFS_POOL=zfs_pool)
    render_file(
        templates_dir / "homelab-zfs-replication.service",
        build_dir / "homelab-zfs-replication.service",
        ZFS_MOUNTPOINT=zfs_mountpoint,
        REPLICATION_SOURCE=replication_source,
        REPLICATION_TARGET=replication_target,
        REPLICATION_POST_HOOK=replication_post_hook,
    )
    render_file(templates_dir / "homelab-zfs-replication.timer", build_dir / "homelab-zfs-replication.timer", REPLICATION_SCHEDULE=replication_schedule)
    render_file(templates_dir / "homelab-zfs-health-check.service", build_dir / "homelab-zfs-health-check.service", ZFS_POOL=zfs_pool)
    render_file(
        templates_dir / "homelab-zfs-health-check.timer",
        build_dir / "homelab-zfs-health-check.timer",
        HEALTH_CHECK_SCHEDULE=health_check_schedule,
    )

    write_env_file(
        build_dir / "env",
        {
            "DEPLOY_USER": deploy_user,
            "ZFS_POOL": zfs_pool,
            "ZFS_MOUNTPOINT": zfs_mountpoint,
            "REBUILD_BUNDLE_ROOT": f"{zfs_mountpoint}/appdata/scripts/zfs-automation",
        },
    )

    artifacts = HostArtifacts(build_dir=build_dir, zfs_mountpoint=zfs_mountpoint, deploy_user=deploy_user, file_specs=FILE_SPECS)
    write_file_map(build_dir, artifacts)
    return artifacts
