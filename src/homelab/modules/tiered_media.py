from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..media_storage import load_media_storage
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-tiered-media"
TEMPLATE_FILES = ["homelab-tiered-media.service"]


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
class TieredMediaConfig:
    branches: tuple[str, ...]
    mountpoint: str
    hdd_only_mountpoint: str | None
    create_policy: str
    min_free_space: str
    consumer_units: tuple[str, ...]


PRIMARY_SERVICE_SPEC = FileSpec(
    "homelab-tiered-media.service",
    "/etc/systemd/system/homelab-tiered-media.service",
)
HDD_SERVICE_SPEC = FileSpec(
    "homelab-tiered-media-hdd.service",
    "/etc/systemd/system/homelab-tiered-media-hdd.service",
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="tiered-media")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping tiered-media (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "tiered-media" / "templates"
    installer = root / "tiered-media" / "scripts" / "install.sh"

    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        normalize_config(registry, host)


def normalize_config(registry, host: str) -> TieredMediaConfig:
    try:
        host_type = str(registry.get(host, "config.type"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type not in {"pve", "ubuntu"}:
        raise ValueError(f"tiered-media supports PVE and Ubuntu hosts only: {host}")

    media_storage = load_media_storage(registry, host)
    branches_raw = registry.get(host, "tiered-media.branches", None)
    if branches_raw is None and media_storage is not None:
        if media_storage.pool_cache_media_path is not None and media_storage.raw_media_branches():
            branches_raw = [media_storage.pool_cache_media_path, *media_storage.raw_media_branches()]
        elif media_storage.export_cache_media_path is not None and media_storage.export_media_branches():
            branches_raw = [media_storage.export_cache_media_path, *media_storage.export_media_branches()]
        else:
            branches_raw = []
    elif branches_raw is None:
        branches_raw = []
    if not isinstance(branches_raw, list) or len(branches_raw) < 2:
        raise ValueError(f"tiered-media.branches must be a list of at least two paths for {host}")

    branches: list[str] = []
    for index, branch in enumerate(branches_raw):
        branch_path = str(branch).strip()
        if not branch_path.startswith("/"):
            raise ValueError(f"tiered-media.branches[{index}] must be an absolute path for {host}")
        if branch_path in branches:
            raise ValueError(f"duplicate tiered-media branch path {branch_path} for {host}")
        branches.append(branch_path)

    mountpoint = str(registry.get(host, "tiered-media.mountpoint", "")).strip()
    if not mountpoint and media_storage is not None:
        mountpoint = media_storage.pool_merged_media_path or media_storage.export_merged_media_path or ""
    if not mountpoint.startswith("/"):
        raise ValueError(f"tiered-media.mountpoint must be an absolute path for {host}")
    if mountpoint in branches:
        raise ValueError(f"tiered-media.mountpoint must differ from branch paths for {host}")

    hdd_only_mountpoint_raw = str(registry.get(host, "tiered-media.hdd_only_mountpoint", "")).strip()
    if not hdd_only_mountpoint_raw and media_storage is not None:
        hdd_only_mountpoint_raw = (
            media_storage.pool_hdd_only_media_path
            or media_storage.export_hdd_only_media_path
            or ""
        )
    hdd_only_mountpoint: str | None = None
    if hdd_only_mountpoint_raw:
        if not hdd_only_mountpoint_raw.startswith("/"):
            raise ValueError(
                f"tiered-media.hdd_only_mountpoint must be an absolute path for {host}"
            )
        if hdd_only_mountpoint_raw in branches:
            raise ValueError(
                f"tiered-media.hdd_only_mountpoint must differ from branch paths for {host}"
            )
        if hdd_only_mountpoint_raw == mountpoint:
            raise ValueError(
                "tiered-media.hdd_only_mountpoint must differ from "
                f"tiered-media.mountpoint for {host}"
            )
        hdd_only_mountpoint = hdd_only_mountpoint_raw

    create_policy = str(registry.get(host, "tiered-media.create_policy", "ff")).strip()
    if not create_policy:
        raise ValueError(f"tiered-media.create_policy must be non-empty for {host}")

    min_free_space = str(registry.get(host, "tiered-media.min_free_space", "100G")).strip()
    if not min_free_space:
        raise ValueError(f"tiered-media.min_free_space must be non-empty for {host}")

    consumer_ctids_raw = registry.get(host, "tiered-media.consumer_ctids", [])
    consumer_units: list[str] = []
    if consumer_ctids_raw not in (None, []):
        if host_type != "pve":
            raise ValueError(f"tiered-media.consumer_ctids is only valid on PVE hosts: {host}")
        if not isinstance(consumer_ctids_raw, list):
            raise ValueError(f"tiered-media.consumer_ctids must be a list for {host}")
        for ctid in consumer_ctids_raw:
            ctid_text = str(ctid).strip()
            if not ctid_text.isdigit():
                raise ValueError(f"tiered-media.consumer_ctids entries must be numeric for {host}")
            consumer_units.append(f"pve-container@{ctid_text}.service")

    return TieredMediaConfig(
        branches=tuple(branches),
        mountpoint=mountpoint,
        hdd_only_mountpoint=hdd_only_mountpoint,
        create_policy=create_policy,
        min_free_space=min_free_space,
        consumer_units=tuple(consumer_units),
    )


def mergerfs_options(config: TieredMediaConfig, *, fsname: str) -> str:
    return ",".join(
        [
            "allow_other",
            "use_ino",
            "cache.files=off",
            "dropcacheonclose=true",
            f"category.create={config.create_policy}",
            f"minfreespace={config.min_free_space}",
            "moveonenospc=true",
            f"fsname={fsname}",
        ]
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    config = normalize_config(registry, host)
    module_dir = root / "tiered-media"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    file_specs = [PRIMARY_SERVICE_SPEC]
    ordering_lines = "\n".join(f"Before={unit}" for unit in config.consumer_units)
    render_file(
        module_dir / "templates" / "homelab-tiered-media.service",
        build_dir / "homelab-tiered-media.service",
        DESCRIPTION="Homelab Tiered Media MergerFS Mount",
        BRANCH_DIRS=" ".join(config.branches),
        BRANCHES=":".join(config.branches),
        MOUNTPOINT=config.mountpoint,
        MERGERFS_OPTIONS=mergerfs_options(config, fsname="homelab-tiered-media"),
        ORDERING_LINES=ordering_lines,
        REQUIRES_MOUNTS_FOR=" ".join(config.branches),
    )

    if config.hdd_only_mountpoint:
        archive_branches = config.branches[1:]
        render_file(
            module_dir / "templates" / "homelab-tiered-media.service",
            build_dir / "homelab-tiered-media-hdd.service",
            DESCRIPTION="Homelab HDD-Only Media MergerFS Mount",
            BRANCH_DIRS=" ".join(archive_branches),
            BRANCHES=":".join(archive_branches),
            MOUNTPOINT=config.hdd_only_mountpoint,
            MERGERFS_OPTIONS=mergerfs_options(config, fsname="homelab-tiered-media-hdd"),
            ORDERING_LINES="",
            REQUIRES_MOUNTS_FOR=" ".join(archive_branches),
        )
        file_specs.append(HDD_SERVICE_SPEC)

    write_file_map(build_dir, tuple(file_specs))
    return HostArtifacts(build_dir=build_dir, file_specs=tuple(file_specs))


def write_file_map(build_dir: Path, file_specs: tuple[FileSpec, ...]) -> None:
    lines = [f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in file_specs]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    artifacts = build_host_artifacts(root, host)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))
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
            (root / "tiered-media" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
