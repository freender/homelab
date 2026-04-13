from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-media-mover"
TEMPLATE_FILES = [
    "homelab-media-mover.service",
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
    tautulli_url: str
    tautulli_config_path: str
    tautulli_lookback_days: int
    frequent_budget: str
    cache_min_free_space: str
    cache_target_free_space: str
    state_file: str


FILE_SPECS = (
    FileSpec("homelab-media-mover.service", "/etc/systemd/system/homelab-media-mover.service"),
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

    source_dir = require_text(
        registry.get(host, "media-mover.source_dir", ""),
        f"media-mover.source_dir is required for {host}",
    )
    target_dir = require_text(
        registry.get(host, "media-mover.target_dir", ""),
        f"media-mover.target_dir is required for {host}",
    )
    if not source_dir.startswith("/") or not target_dir.startswith("/"):
        raise ValueError(f"media-mover paths must be absolute for {host}")
    if source_dir == target_dir:
        raise ValueError(f"media-mover source_dir and target_dir must differ for {host}")

    schedule = require_text(
        registry.get(host, "media-mover.schedule", "daily"),
        f"media-mover.schedule is required for {host}",
    )

    managed_roots_raw = registry.get(host, "media-mover.managed_roots", ["movies", "tv"])
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

    merged_root = require_text(
        registry.get(host, "media-mover.merged_root", "/mnt/user/media"),
        f"media-mover.merged_root is required for {host}",
    )
    plex_mount_root = require_text(
        registry.get(host, "media-mover.plex_mount_root", "/data"),
        f"media-mover.plex_mount_root is required for {host}",
    )
    tautulli_url = require_text(
        registry.get(host, "media-mover.tautulli_url", "https://tautulli.freender.net"),
        f"media-mover.tautulli_url is required for {host}",
    )
    tautulli_config_path = require_text(
        registry.get(
            host,
            "media-mover.tautulli_config_path",
            "/mnt/cache/appdata/tautulli/config.ini",
        ),
        f"media-mover.tautulli_config_path is required for {host}",
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

    state_file = require_text(
        registry.get(host, "media-mover.state_file", "/var/lib/homelab-media-mover/state.json"),
        f"media-mover.state_file is required for {host}",
    )

    ignore_paths_raw = registry.get(host, "media-mover.ignore_paths", [])
    if ignore_paths_raw in (None, ""):
        ignore_paths = []
    elif not isinstance(ignore_paths_raw, list):
        raise ValueError(f"media-mover.ignore_paths must be a list for {host}")
    else:
        ignore_paths = []
        for item in ignore_paths_raw:
            path = require_text(
                item,
                f"media-mover.ignore_paths entries must be non-empty for {host}",
            )
            if not path.startswith("/"):
                raise ValueError(f"media-mover.ignore_paths entries must be absolute for {host}")
            ignore_paths.append(path)

    return MediaMoverConfig(
        source_dir=source_dir,
        target_dir=target_dir,
        schedule=schedule,
        ignore_paths=tuple(ignore_paths),
        managed_roots=tuple(managed_roots),
        merged_root=merged_root,
        plex_mount_root=plex_mount_root,
        tautulli_url=tautulli_url,
        tautulli_config_path=tautulli_config_path,
        tautulli_lookback_days=tautulli_lookback_days,
        frequent_budget=frequent_budget,
        cache_min_free_space=cache_min_free_space,
        cache_target_free_space=cache_target_free_space,
        state_file=state_file,
    )


def write_file_map(build_dir: Path) -> None:
    lines = [f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in FILE_SPECS]
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
            "TAUTULLI_URL": config.tautulli_url,
            "TAUTULLI_CONFIG_PATH": config.tautulli_config_path,
            "TAUTULLI_LOOKBACK_DAYS": str(config.tautulli_lookback_days),
            "FREQUENT_BUDGET": config.frequent_budget,
            "CACHE_MIN_FREE_SPACE": config.cache_min_free_space,
            "CACHE_TARGET_FREE_SPACE": config.cache_target_free_space,
            "STATE_FILE": config.state_file,
        },
    )
    write_file_map(build_dir)
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
            (root / "media-mover" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
