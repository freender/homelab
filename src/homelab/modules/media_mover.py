from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import copy_file, render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..media_storage import load_media_storage
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-media-mover"
TEMPLATE_FILES = [
    "homelab-media-mover.service",
    "homelab-media-mover-now.service",
    "homelab-media-mover.timer",
    "homelab-media-mover.py",
]


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
class MediaMoverConfig:
    source_dir: str
    target_dir: str
    schedule: str
    ignore_paths: tuple[str, ...]
    managed_roots: tuple[str, ...]
    merged_root: str
    plex_mount_root: str
    tautulli_lookback_days: int
    frequent_budget: str
    cache_min_free_space: str
    cache_target_free_space: str
    min_file_age: str
    state_file: str
    dependency_units: tuple[str, ...]


FILE_SPECS = (
    FileSpec("homelab-media-mover.service", "/etc/systemd/system/homelab-media-mover.service"),
    FileSpec(
        "homelab-media-mover-now.service",
        "/etc/systemd/system/homelab-media-mover-now.service",
    ),
    FileSpec("homelab-media-mover.timer", "/etc/systemd/system/homelab-media-mover.timer"),
    FileSpec("homelab-media-mover.py", "/usr/local/bin/homelab-media-mover", mode="755"),
    FileSpec("media-mover.env", "/etc/default/homelab-media-mover", mode="600"),
)
LOCAL_ENV_SPEC = FileSpec(
    "media-mover.local.env",
    "/etc/default/homelab-media-mover.local",
    mode="600",
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="media-mover")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping media-mover (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "media-mover" / "templates"
    installer = root / "media-mover" / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        normalize_config(registry, host)


def require_text(value: object, message: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(message)
    return text


def normalize_config(registry, host: str) -> MediaMoverConfig:
    try:
        host_type = str(registry.get(host, "config.type"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type not in {"pve", "ubuntu"}:
        raise ValueError(f"media-mover supports PVE and Ubuntu hosts only: {host}")

    media_storage = load_media_storage(registry, host)

    source_dir_value = registry.get(host, "media-mover.source_dir", "")
    if not str(source_dir_value).strip() and media_storage is not None:
        source_dir_value = (
            media_storage.pool_cache_media_path
            or media_storage.export_cache_media_path
            or ""
        )
    source_dir = require_text(source_dir_value, f"media-mover.source_dir is required for {host}")

    target_dir_value = registry.get(host, "media-mover.target_dir", "")
    if not str(target_dir_value).strip() and media_storage is not None:
        target_dir_value = (
            media_storage.pool_hdd_only_media_path
            or media_storage.export_hdd_only_media_path
            or ""
        )
    target_dir = require_text(target_dir_value, f"media-mover.target_dir is required for {host}")
    if not source_dir.startswith("/") or not target_dir.startswith("/"):
        raise ValueError(f"media-mover paths must be absolute for {host}")
    if source_dir == target_dir:
        raise ValueError(f"media-mover source_dir and target_dir must differ for {host}")

    schedule = require_text(
        registry.get(host, "media-mover.schedule", "daily"),
        f"media-mover.schedule is required for {host}",
    )

    managed_roots_raw = registry.get(host, "media-mover.managed_roots", ["movies", "movies4k", "tv", "tv4k"])
    if not isinstance(managed_roots_raw, list) or not managed_roots_raw:
        raise ValueError(f"media-mover.managed_roots must be a non-empty list for {host}")
    managed_roots = []
    for item in managed_roots_raw:
        root_name = require_text(
            item,
            f"media-mover.managed_roots entries must be non-empty for {host}",
        )
        if "/" in root_name:
            raise ValueError(
                f"media-mover.managed_roots entries must be simple relative names for {host}"
            )
        managed_roots.append(root_name)

    merged_root_value = registry.get(host, "media-mover.merged_root", "")
    if not str(merged_root_value).strip() and media_storage is not None:
        merged_root_value = (
            media_storage.pool_merged_media_path
            or media_storage.export_merged_media_path
            or ""
        )
    merged_root = require_text(merged_root_value, f"media-mover.merged_root is required for {host}")
    if target_dir == merged_root:
        raise ValueError(
            "media-mover.target_dir must not use the merged media path "
            f"for {host}; use an HDD-only path"
        )
    plex_mount_root = require_text(
        registry.get(host, "media-mover.plex_mount_root", "/data"),
        f"media-mover.plex_mount_root is required for {host}",
    )
    lookback_days_raw = registry.get(host, "media-mover.tautulli_lookback_days", 90)
    try:
        tautulli_lookback_days = int(lookback_days_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"media-mover.tautulli_lookback_days must be an integer for {host}"
        ) from exc
    if tautulli_lookback_days < 1:
        raise ValueError(f"media-mover.tautulli_lookback_days must be at least 1 for {host}")

    frequent_budget = require_text(
        registry.get(host, "media-mover.frequent_budget", "500G"),
        f"media-mover.frequent_budget is required for {host}",
    )

    cache_min_free_space = require_text(
        registry.get(host, "media-mover.cache_min_free_space", "500G"),
        f"media-mover.cache_min_free_space is required for {host}",
    )
    cache_target_free_space = require_text(
        registry.get(host, "media-mover.cache_target_free_space", "700G"),
        f"media-mover.cache_target_free_space is required for {host}",
    )
    min_file_age = require_text(
        registry.get(host, "media-mover.min_file_age", "5m"),
        f"media-mover.min_file_age is required for {host}",
    )

    state_file = require_text(
        registry.get(host, "media-mover.state_file", "/var/lib/homelab-media-mover/state.json"),
        f"media-mover.state_file is required for {host}",
    )

    dependency_units: list[str] = []
    tiered_media_mountpoint = str(registry.get(host, "tiered-media.mountpoint", "")).strip()
    if not tiered_media_mountpoint and media_storage is not None:
        tiered_media_mountpoint = (
            media_storage.pool_merged_media_path
            or media_storage.export_merged_media_path
            or ""
        )
    if tiered_media_mountpoint and merged_root == tiered_media_mountpoint:
        dependency_units.append("homelab-tiered-media.service")

    tiered_media_hdd_mountpoint = str(registry.get(host, "tiered-media.hdd_only_mountpoint", "")).strip()
    if not tiered_media_hdd_mountpoint and media_storage is not None:
        tiered_media_hdd_mountpoint = (
            media_storage.pool_hdd_only_media_path
            or media_storage.export_hdd_only_media_path
            or ""
        )
    if tiered_media_hdd_mountpoint and target_dir == tiered_media_hdd_mountpoint:
        dependency_units.append("homelab-tiered-media-hdd.service")

    ignore_paths = normalize_ignore_paths(registry, host, source_dir)

    return MediaMoverConfig(
        source_dir=source_dir,
        target_dir=target_dir,
        schedule=schedule,
        ignore_paths=tuple(ignore_paths),
        managed_roots=tuple(managed_roots),
        merged_root=merged_root,
        plex_mount_root=plex_mount_root,
        tautulli_lookback_days=tautulli_lookback_days,
        frequent_budget=frequent_budget,
        cache_min_free_space=cache_min_free_space,
        cache_target_free_space=cache_target_free_space,
        min_file_age=min_file_age,
        state_file=state_file,
        dependency_units=tuple(dependency_units),
    )


def normalize_ignore_paths(registry, host: str, source_dir: str) -> tuple[str, ...]:
    source_root = Path(source_dir)
    ignore_relative_paths_raw = registry.get(host, "media-mover.ignore_relative_paths", None)
    ignore_paths_raw = registry.get(host, "media-mover.ignore_paths", None)

    if ignore_relative_paths_raw not in (None, ""):
        if not isinstance(ignore_relative_paths_raw, list):
            raise ValueError(f"media-mover.ignore_relative_paths must be a list for {host}")
        ignore_paths: list[str] = []
        for item in ignore_relative_paths_raw:
            relative_text = require_text(
                item,
                f"media-mover.ignore_relative_paths entries must be non-empty for {host}",
            )
            relative_path = Path(relative_text)
            if relative_path.is_absolute():
                raise ValueError(
                    f"media-mover.ignore_relative_paths entries must be relative for {host}"
                )
            if any(part == ".." for part in relative_path.parts):
                raise ValueError(
                    "media-mover.ignore_relative_paths entries must stay under the source dir "
                    f"for {host}"
                )
            ignore_paths.append(str(source_root / relative_path))
        return tuple(ignore_paths)

    if ignore_paths_raw in (None, ""):
        return ()
    if not isinstance(ignore_paths_raw, list):
        raise ValueError(f"media-mover.ignore_paths must be a list for {host}")

    ignore_paths = []
    for item in ignore_paths_raw:
        path_text = require_text(
            item,
            f"media-mover.ignore_paths entries must be non-empty for {host}",
        )
        path = Path(path_text)
        if not path.is_absolute():
            raise ValueError(f"media-mover.ignore_paths entries must be absolute for {host}")
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(
                f"media-mover.ignore_paths entries must stay under media-mover.source_dir for {host}"
            ) from exc
        ignore_paths.append(path_text)
    return tuple(ignore_paths)


def write_file_map(build_dir: Path, file_specs: tuple[FileSpec, ...]) -> None:
    lines = [f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in file_specs]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    config = normalize_config(registry, host)
    module_dir = root / "media-mover"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        module_dir / "templates" / "homelab-media-mover.service",
        build_dir / "homelab-media-mover.service",
        SERVICE_DEPENDENCY_LINES=service_dependency_lines(config.dependency_units),
    )
    render_file(
        module_dir / "templates" / "homelab-media-mover-now.service",
        build_dir / "homelab-media-mover-now.service",
        SERVICE_DEPENDENCY_LINES=service_dependency_lines(config.dependency_units),
    )
    render_file(
        module_dir / "templates" / "homelab-media-mover.timer",
        build_dir / "homelab-media-mover.timer",
        MEDIA_MOVER_SCHEDULE=config.schedule,
    )
    render_file(
        module_dir / "templates" / "homelab-media-mover.py",
        build_dir / "homelab-media-mover.py",
    )
    write_env_file(
        build_dir / "media-mover.env",
        {
            "SOURCE_DIR": config.source_dir,
            "TARGET_DIR": config.target_dir,
            "IGNORE_PATHS": ":".join(config.ignore_paths),
            "MANAGED_ROOTS": ":".join(config.managed_roots),
            "MERGED_ROOT": config.merged_root,
            "PLEX_MOUNT_ROOT": config.plex_mount_root,
            "TAUTULLI_LOOKBACK_DAYS": str(config.tautulli_lookback_days),
            "FREQUENT_BUDGET": config.frequent_budget,
            "CACHE_MIN_FREE_SPACE": config.cache_min_free_space,
            "CACHE_TARGET_FREE_SPACE": config.cache_target_free_space,
            "MIN_FILE_AGE": config.min_file_age,
            "STATE_FILE": config.state_file,
        },
    )
    file_specs = list(FILE_SPECS)
    local_env_path = module_dir / ".env"
    if local_env_path.is_file():
        copy_file(local_env_path, build_dir / LOCAL_ENV_SPEC.build_name)
        file_specs.append(LOCAL_ENV_SPEC)
    write_file_map(build_dir, tuple(file_specs))
    return HostArtifacts(build_dir=build_dir, file_specs=tuple(file_specs))


def service_dependency_lines(units: tuple[str, ...]) -> str:
    if not units:
        return ""
    joined_units = " ".join(units)
    return f"Wants={joined_units}\nAfter={joined_units}"


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
            (root / "media-mover" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
