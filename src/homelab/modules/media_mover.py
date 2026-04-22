from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..media_storage import load_media_storage
from ..module_support import FileSpec, HostArtifacts, require_text, write_file_map
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-media-mover"
DEFAULT_MEDIA_MOVER_SCHEDULE = "*-*-* 00:00:00"
TEMPLATE_FILES = [
    "homelab-media-mover.service",
    "homelab-media-mover-watch.service",
    "homelab-media-mover.timer",
    "homelab-media-mover.py",
]


@dataclass(frozen=True)
class MediaMoverConfig:
    source_dir: str
    target_dir: str
    schedule: str
    ignore_paths: tuple[str, ...]
    managed_roots: tuple[str, ...]
    merged_root: str
    plex_mount_root: str
    plex_url: str
    tautulli_config_path: str
    ondeck_enabled: bool
    ondeck_budget: str
    ondeck_tv_prefetch_episodes: int
    ondeck_include_movies: bool
    ondeck_movie_max_age_days: int
    ondeck_series_max_age_days: int
    recent_movie_retention_days: int
    recent_tv_retention_days: int
    cache_min_free_space: str
    cache_target_free_space: str
    min_file_age: str
    state_file: str
    dependency_units: tuple[str, ...]
    manage_timer: bool


FILE_SPECS = (
    FileSpec("homelab-media-mover.service", "/etc/systemd/system/homelab-media-mover.service"),
    FileSpec(
        "homelab-media-mover-watch.service",
        "/etc/systemd/system/homelab-media-mover-watch.service",
    ),
    FileSpec("homelab-media-mover.timer", "/etc/systemd/system/homelab-media-mover.timer"),
    FileSpec("homelab-media-mover.py", "/usr/local/bin/homelab-media-mover", mode="755"),
    FileSpec("media-mover.env", "/etc/default/homelab-media-mover", mode="600"),
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
        source_dir_value = media_storage.preferred_cache_media_path(host_type) or ""
    source_dir = require_text(source_dir_value, f"media-mover.source_dir is required for {host}")

    target_dir_value = registry.get(host, "media-mover.target_dir", "")
    if not str(target_dir_value).strip() and media_storage is not None:
        target_dir_value = media_storage.preferred_hdd_only_media_path(host_type) or ""
    target_dir = require_text(target_dir_value, f"media-mover.target_dir is required for {host}")
    if not source_dir.startswith("/") or not target_dir.startswith("/"):
        raise ValueError(f"media-mover paths must be absolute for {host}")
    if source_dir == target_dir:
        raise ValueError(f"media-mover source_dir and target_dir must differ for {host}")

    manage_timer = str(registry.get(host, "media-mover.manage_timer", "true")).lower() == "true"
    schedule_raw = registry.get(host, "media-mover.schedule", None)
    if schedule_raw in (None, ""):
        if manage_timer:
            raise ValueError(f"media-mover.schedule is required for {host}")
        schedule = DEFAULT_MEDIA_MOVER_SCHEDULE
    else:
        schedule = normalize_schedule(schedule_raw)

    managed_roots_raw = registry.get(
        host,
        "media-mover.managed_roots",
        ["movies", "movies4k", "tv", "tv4k"],
    )
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
        merged_root_value = media_storage.preferred_merged_media_path(host_type) or ""
    merged_root = require_text(
        merged_root_value,
        f"media-mover.merged_root is required for {host}",
    )
    if target_dir == merged_root:
        raise ValueError(
            "media-mover.target_dir must not use the merged media path "
            f"for {host}; use an HDD-only path"
        )
    plex_mount_root = require_text(
        registry.get(host, "media-mover.plex_mount_root", "/data"),
        f"media-mover.plex_mount_root is required for {host}",
    )
    plex_url = require_text(
        registry.get(host, "media-mover.plex_url", "http://127.0.0.1:32400"),
        f"media-mover.plex_url is required for {host}",
    )
    tautulli_config_path = require_text(
        registry.get(
            host,
            "media-mover.tautulli_config_path",
            "/mnt/cache/appdata/tautulli/config.ini",
        ),
        f"media-mover.tautulli_config_path is required for {host}",
    )
    ondeck_enabled = str(registry.get(host, "media-mover.ondeck_enabled", "true")).lower() == "true"
    ondeck_budget = require_text(
        registry.get(host, "media-mover.ondeck_budget", "250G"),
        f"media-mover.ondeck_budget is required for {host}",
    )
    ondeck_tv_prefetch_raw = registry.get(host, "media-mover.ondeck_tv_prefetch_episodes", 1)
    try:
        ondeck_tv_prefetch_episodes = int(ondeck_tv_prefetch_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"media-mover.ondeck_tv_prefetch_episodes must be an integer for {host}"
        ) from exc
    if ondeck_tv_prefetch_episodes < 0:
        raise ValueError(
            f"media-mover.ondeck_tv_prefetch_episodes must be at least 0 for {host}"
        )
    ondeck_include_movies = (
        str(registry.get(host, "media-mover.ondeck_include_movies", "false")).lower() == "true"
    )
    ondeck_movie_max_age_raw = registry.get(host, "media-mover.ondeck_movie_max_age_days", 30)
    try:
        ondeck_movie_max_age_days = int(ondeck_movie_max_age_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"media-mover.ondeck_movie_max_age_days must be an integer for {host}"
        ) from exc
    if ondeck_movie_max_age_days < 1:
        raise ValueError(
            f"media-mover.ondeck_movie_max_age_days must be at least 1 for {host}"
        )
    ondeck_series_max_age_raw = registry.get(host, "media-mover.ondeck_series_max_age_days", 60)
    try:
        ondeck_series_max_age_days = int(ondeck_series_max_age_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"media-mover.ondeck_series_max_age_days must be an integer for {host}"
        ) from exc
    if ondeck_series_max_age_days < 1:
        raise ValueError(
            f"media-mover.ondeck_series_max_age_days must be at least 1 for {host}"
        )
    recent_movie_retention_raw = registry.get(host, "media-mover.recent_movie_retention_days", 14)
    try:
        recent_movie_retention_days = int(recent_movie_retention_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"media-mover.recent_movie_retention_days must be an integer for {host}"
        ) from exc
    if recent_movie_retention_days < 0:
        raise ValueError(
            f"media-mover.recent_movie_retention_days must be at least 0 for {host}"
        )

    recent_tv_retention_raw = registry.get(host, "media-mover.recent_tv_retention_days", 14)
    try:
        recent_tv_retention_days = int(recent_tv_retention_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"media-mover.recent_tv_retention_days must be an integer for {host}"
        ) from exc
    if recent_tv_retention_days < 0:
        raise ValueError(
            f"media-mover.recent_tv_retention_days must be at least 0 for {host}"
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
    tiered_media_mountpoint = str(registry.get(host, "media-pool.mountpoint", "")).strip()
    if not tiered_media_mountpoint and media_storage is not None:
        tiered_media_mountpoint = media_storage.preferred_merged_media_path(host_type) or ""
    if tiered_media_mountpoint and merged_root == tiered_media_mountpoint:
        dependency_units.append("homelab-media-pool.service")

    tiered_media_hdd_mountpoint = str(
        registry.get(host, "media-pool.hdd_only_mountpoint", "")
    ).strip()
    if not tiered_media_hdd_mountpoint and media_storage is not None:
        tiered_media_hdd_mountpoint = media_storage.preferred_hdd_only_media_path(host_type) or ""
    if tiered_media_hdd_mountpoint and target_dir == tiered_media_hdd_mountpoint:
        dependency_units.append("homelab-media-pool-hdd-only.service")

    ignore_paths = normalize_ignore_paths(registry, host, source_dir)

    return MediaMoverConfig(
        source_dir=source_dir,
        target_dir=target_dir,
        schedule=schedule,
        ignore_paths=tuple(ignore_paths),
        managed_roots=tuple(managed_roots),
        merged_root=merged_root,
        plex_mount_root=plex_mount_root,
        plex_url=plex_url,
        tautulli_config_path=tautulli_config_path,
        ondeck_enabled=ondeck_enabled,
        ondeck_budget=ondeck_budget,
        ondeck_tv_prefetch_episodes=ondeck_tv_prefetch_episodes,
        ondeck_include_movies=ondeck_include_movies,
        ondeck_movie_max_age_days=ondeck_movie_max_age_days,
        ondeck_series_max_age_days=ondeck_series_max_age_days,
        recent_movie_retention_days=recent_movie_retention_days,
        recent_tv_retention_days=recent_tv_retention_days,
        cache_min_free_space=cache_min_free_space,
        cache_target_free_space=cache_target_free_space,
        min_file_age=min_file_age,
        state_file=state_file,
        dependency_units=tuple(dependency_units),
        manage_timer=manage_timer,
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
                "media-mover.ignore_paths entries must stay under "
                f"media-mover.source_dir for {host}"
            ) from exc
        ignore_paths.append(path_text)
    return tuple(ignore_paths)


def normalize_schedule(value: object) -> str:
    schedule = str(value).strip()
    if schedule.lower() in {"daily", "dayly"}:
        return DEFAULT_MEDIA_MOVER_SCHEDULE
    return require_text(schedule, "media-mover.schedule is required")


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
        module_dir / "templates" / "homelab-media-mover-watch.service",
        build_dir / "homelab-media-mover-watch.service",
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
            "PLEX_URL": config.plex_url,
            "TAUTULLI_CONFIG_PATH": config.tautulli_config_path,
            "ONDECK_ENABLED": "true" if config.ondeck_enabled else "false",
            "ONDECK_BUDGET": config.ondeck_budget,
            "ONDECK_TV_PREFETCH_EPISODES": str(config.ondeck_tv_prefetch_episodes),
            "ONDECK_INCLUDE_MOVIES": "true" if config.ondeck_include_movies else "false",
            "ONDECK_MOVIE_MAX_AGE_DAYS": str(config.ondeck_movie_max_age_days),
            "ONDECK_SERIES_MAX_AGE_DAYS": str(config.ondeck_series_max_age_days),
            "RECENT_MOVIE_RETENTION_DAYS": str(config.recent_movie_retention_days),
            "RECENT_TV_RETENTION_DAYS": str(config.recent_tv_retention_days),
            "CACHE_MIN_FREE_SPACE": config.cache_min_free_space,
            "CACHE_TARGET_FREE_SPACE": config.cache_target_free_space,
            "MIN_FILE_AGE": config.min_file_age,
            "STATE_FILE": config.state_file,
            "ENABLE_MEDIA_MOVER_TIMER": "true" if config.manage_timer else "false",
        },
    )
    write_file_map(build_dir, FILE_SPECS)
    return HostArtifacts(build_dir=build_dir, file_specs=FILE_SPECS)


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
