from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..media_storage import load_media_storage
from ..module_support import FileSpec, HostArtifacts, require_text, write_file_map
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-snapraid"
TEMPLATE_FILES = [
    "snapraid.conf",
    "homelab-snapraid-sync.service",
    "homelab-snapraid-sync.timer",
    "homelab-snapraid-scrub.service",
    "homelab-snapraid-scrub.timer",
    "homelab-snapraid-status-notify",
]


@dataclass(frozen=True)
class SnapRaidDisk:
    name: str
    path: str


@dataclass(frozen=True)
class SnapRaidConfig:
    data_disks: tuple[SnapRaidDisk, ...]
    parity_disks: tuple[SnapRaidDisk, ...]
    content_files: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    pool_path: str
    sync_schedule: str
    scrub_schedule: str
    orchestrate_media_mover: bool


FILE_SPECS = (
    FileSpec("snapraid.conf", "/etc/snapraid.conf"),
    FileSpec("homelab-snapraid-sync.service", "/etc/systemd/system/homelab-snapraid-sync.service"),
    FileSpec("homelab-snapraid-sync.timer", "/etc/systemd/system/homelab-snapraid-sync.timer"),
    FileSpec(
        "homelab-snapraid-scrub.service",
        "/etc/systemd/system/homelab-snapraid-scrub.service",
    ),
    FileSpec("homelab-snapraid-scrub.timer", "/etc/systemd/system/homelab-snapraid-scrub.timer"),
    FileSpec(
        "homelab-snapraid-status-notify",
        "/usr/local/bin/homelab-snapraid-status-notify",
        mode="755",
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
    supported_hosts = registry.list_hosts(feature="snapraid")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping snapraid (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "snapraid" / "templates"
    installer = root / "snapraid" / "scripts" / "install.sh"

    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        normalize_config(registry, host)


def normalize_config(registry, host: str) -> SnapRaidConfig:
    media_storage = load_media_storage(registry, host)
    data_disks_raw = registry.get(host, "snapraid.data_disks", None)
    if data_disks_raw is None and media_storage is not None:
        data_disks_raw = [
            {"name": name, "path": path}
            for name, path in media_storage.raw_data_disks()
        ]
    elif data_disks_raw is None:
        data_disks_raw = []
    if not isinstance(data_disks_raw, list) or not data_disks_raw:
        raise ValueError(f"snapraid.data_disks must be a non-empty list for {host}")

    data_disks: list[SnapRaidDisk] = []
    for item in data_disks_raw:
        if not isinstance(item, dict) or "name" not in item or "path" not in item:
            raise ValueError(f"snapraid.data_disks entry must have name and path for {host}")
        name = require_text(item["name"], f"snapraid.data_disks name must be non-empty for {host}")
        path = require_text(item["path"], f"snapraid.data_disks path must be non-empty for {host}")
        if not path.startswith("/"):
            raise ValueError(f"snapraid.data_disks path must be absolute for {host}")
        data_disks.append(SnapRaidDisk(name=name, path=path))

    parity_disks_raw = registry.get(host, "snapraid.parity_disks", None)
    if parity_disks_raw is None and media_storage is not None:
        parity_disks_raw = [
            {"name": name, "path": path}
            for name, path in media_storage.raw_parity_disks()
        ]
    elif parity_disks_raw is None:
        parity_disks_raw = []
    if not isinstance(parity_disks_raw, list) or not parity_disks_raw:
        raise ValueError(f"snapraid.parity_disks must be a non-empty list for {host}")

    parity_disks: list[SnapRaidDisk] = []
    for item in parity_disks_raw:
        if not isinstance(item, dict) or "name" not in item or "path" not in item:
            raise ValueError(f"snapraid.parity_disks entry must have name and path for {host}")
        name = require_text(
            item["name"],
            f"snapraid.parity_disks name must be non-empty for {host}",
        )
        path = require_text(
            item["path"],
            f"snapraid.parity_disks path must be non-empty for {host}",
        )
        if not path.startswith("/"):
            raise ValueError(f"snapraid.parity_disks path must be absolute for {host}")
        parity_disks.append(SnapRaidDisk(name=name, path=path))

    content_files_raw = registry.get(host, "snapraid.content_files", None)
    if content_files_raw is None and media_storage is not None:
        content_files_raw = list(media_storage.raw_content_files())
    elif content_files_raw is None:
        content_files_raw = []
    if not isinstance(content_files_raw, list) or not content_files_raw:
        raise ValueError(f"snapraid.content_files must be a non-empty list for {host}")
    content_files = []
    for item in content_files_raw:
        path = require_text(item, f"snapraid.content_files entries must be non-empty for {host}")
        if not path.startswith("/"):
            raise ValueError(f"snapraid.content_files entries must be absolute paths for {host}")
        content_files.append(path)

    exclude_patterns_raw = registry.get(
        host,
        "snapraid.exclude_patterns",
        ["*.unrecoverable", "/tmp/", "/lost+found/"],
    )
    if not isinstance(exclude_patterns_raw, list):
        raise ValueError(f"snapraid.exclude_patterns must be a list for {host}")
    exclude_patterns = tuple(
        require_text(item, f"snapraid.exclude_patterns entries must be non-empty for {host}")
        for item in exclude_patterns_raw
    )

    pool_path_value = registry.get(host, "snapraid.pool_path", "")
    if not str(pool_path_value).strip() and media_storage is not None:
        pool_path_value = media_storage.pool_root_path or ""
    pool_path = require_text(pool_path_value, f"snapraid.pool_path is required for {host}")
    if not pool_path.startswith("/"):
        raise ValueError(f"snapraid.pool_path must be an absolute path for {host}")

    sync_schedule = require_text(
        registry.get(host, "snapraid.sync_schedule", "daily"),
        f"snapraid.sync_schedule is required for {host}",
    )
    scrub_schedule = require_text(
        registry.get(host, "snapraid.scrub_schedule", "Sun *-*-* 09:00:00"),
        f"snapraid.scrub_schedule is required for {host}",
    )
    has_media_mover = host in registry.list_hosts(feature="media-mover")
    orchestrate_media_mover = (
        str(
            registry.get(
                host,
                "snapraid.orchestrate_media_mover",
                "true" if has_media_mover else "false",
            )
        ).lower()
        == "true"
    )
    if orchestrate_media_mover and not has_media_mover:
        raise ValueError(f"snapraid.orchestrate_media_mover requires media-mover on {host}")

    return SnapRaidConfig(
        data_disks=tuple(data_disks),
        parity_disks=tuple(parity_disks),
        content_files=tuple(content_files),
        exclude_patterns=exclude_patterns,
        pool_path=pool_path,
        sync_schedule=sync_schedule,
        scrub_schedule=scrub_schedule,
        orchestrate_media_mover=orchestrate_media_mover,
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    config = normalize_config(registry, host)
    module_dir = root / "snapraid"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        module_dir / "templates" / "snapraid.conf",
        build_dir / "snapraid.conf",
        DATA_DISKS=config.data_disks,
        PARITY_DISKS=config.parity_disks,
        CONTENT_FILES=config.content_files,
        EXCLUDE_PATTERNS=config.exclude_patterns,
        POOL_PATH=config.pool_path,
    )

    # Use render_file for simple copies if no vars needed, or use a simple copy if preferred
    # But these templates might have vars later (e.g. schedule)
    for tmpl in [
        "homelab-snapraid-sync.service",
        "homelab-snapraid-sync.timer",
        "homelab-snapraid-scrub.service",
        "homelab-snapraid-scrub.timer",
    ]:
        context = {}
        if tmpl == "homelab-snapraid-sync.timer":
            context["SYNC_SCHEDULE"] = config.sync_schedule
        elif tmpl == "homelab-snapraid-scrub.timer":
            context["SCRUB_SCHEDULE"] = config.scrub_schedule
        else:
            context["ORCHESTRATE_MEDIA_MOVER"] = config.orchestrate_media_mover
        render_file(module_dir / "templates" / tmpl, build_dir / tmpl, **context)

    render_file(
        module_dir / "templates" / "homelab-snapraid-status-notify",
        build_dir / "homelab-snapraid-status-notify",
    )

    write_file_map(build_dir, FILE_SPECS)
    return HostArtifacts(build_dir=build_dir, file_specs=FILE_SPECS)


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
            (root / "snapraid" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
