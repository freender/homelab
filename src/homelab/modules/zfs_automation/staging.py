"""Per-host build + deploy: render the file map and run install.py.

This is the module's `deploy_host`/`build_host_artifacts` — it turns the typed
config objects from `.normalize`/`.pools` and the rendered scripts from
`.render` into an actual build directory and remote deploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...build import copy_files, render_file, write_env_file
from ...deploy import force_env, prepare_build_dir, stage_and_run_remote_installer
from ...hosts import default_registry
from ...module_support import feature_paused
from ...output import print_sub
from ...ssh import HostConnection, build_files, diff_many
from .normalize import normalize_bool, normalize_snapshot_plans
from .pools import resolve_pools
from .render import build_sanoid_config, build_snapshot_script, shell_array_block
from .types import (
    BASE_FILE_SPECS,
    INSTALLER,
    REMOTE_ROOT,
    STATIC_CONFIG_FILES,
    HostArtifacts,
)


def write_file_map(build_dir: Path, artifacts: HostArtifacts) -> None:
    lines = [f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in artifacts.file_specs]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def diff_pairs_for(artifacts: HostArtifacts) -> list[tuple[Path, str]]:
    return [
        (artifacts.build_dir / spec.build_name, spec.remote_path) for spec in artifacts.file_specs
    ]


def upload_paths_for(
    module_dir: Path, host: str, artifacts: HostArtifacts
) -> list[tuple[Path, str]]:
    return [
        (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]


def report_dry_run(registry: Any, host: str, artifacts: HostArtifacts) -> None:
    """Print what a real deploy would do, including whether it would pause the timers."""
    if feature_paused(registry, host, "zfs-automation"):
        print_sub(
            f"[DRY-RUN] Would pause zfs-automation on {host} "
            "(stop and disable the snapshot and scrub timers)"
        )
    print_sub(f"[DRY-RUN] Would deploy zfs-automation to {host}")
    print_sub("Build files:")
    for file_name in build_files(artifacts.build_dir):
        print_sub(f"    {file_name}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))

    module_dir = root / "zfs-automation"
    artifacts = build_host_artifacts(root, host)
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    print_sub("Comparing with remote configs...")
    for message in diff_many(connection, diff_pairs_for(artifacts)):
        print_sub(message)

    if dry_run:
        report_dry_run(registry, host, artifacts)
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        upload_paths_for(module_dir, host, artifacts),
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "src"),
    )


def _flag(value: object) -> str:
    """Render a truthiness check as the "true"/"false" strings install.py reads."""
    return "true" if value else "false"


@dataclass(frozen=True)
class _HostSettings:
    """The hosts.conf-derived scalars for one host, all normalized in one place."""

    homelab_state_dir: str
    snapshot_schedule: str
    manage_snapshots: bool
    manage_scrub: bool
    paused: bool


def _read_host_settings(registry: Any, host: str) -> _HostSettings:
    """Read and normalize every scalar knob, so the flags fail here or not at all.

    `paused: true` stops and disables both managed zfs timers (snapshots and
    scrub) while keeping the module deployed; distinct from `deploy: false`,
    which skips the host entirely. It is a single host-wide freeze switch that
    overrides the per-area manage_* flags.
    """
    return _HostSettings(
        homelab_state_dir=str(
            registry.get(host, "config.homelab_state_dir", "/var/lib/homelab")
        ),
        snapshot_schedule=str(
            registry.get(host, "zfs-automation.snapshot_schedule", "*-*-* 00:00:00")
        ),
        manage_snapshots=normalize_bool(
            registry.get(host, "zfs-automation.manage_snapshots", None),
            True,
            f"zfs-automation.manage_snapshots must be true or false for {host}",
        ),
        manage_scrub=normalize_bool(
            registry.get(host, "zfs-automation.manage_scrub", None),
            True,
            f"zfs-automation.manage_scrub must be true or false for {host}",
        ),
        paused=feature_paused(registry, host, "zfs-automation"),
    )


def _write_snapshot_artifacts(
    templates_dir: Path,
    build_dir: Path,
    snapshot_plans: Any,
    snapshot_schedule: str,
) -> None:
    """sanoid.conf plus the snapshot unit, timer, and driver script."""
    (build_dir / "sanoid.conf").write_text(
        build_sanoid_config(snapshot_plans),
        encoding="utf-8",
    )
    render_file(
        templates_dir / "homelab-zfs-snapshots.service",
        build_dir / "homelab-zfs-snapshots.service",
    )
    render_file(
        templates_dir / "homelab-zfs-snapshots.timer",
        build_dir / "homelab-zfs-snapshots.timer",
        SNAPSHOT_SCHEDULE=snapshot_schedule,
    )
    (build_dir / "homelab-zfs-snapshots.sh").write_text(
        build_snapshot_script(snapshot_plans),
        encoding="utf-8",
    )


def _write_scrub_artifacts(templates_dir: Path, build_dir: Path, pools: list[str]) -> None:
    """The scrub script (pool list baked in) and its systemd unit."""
    render_file(
        templates_dir / "homelab-zfs-scrub.sh",
        build_dir / "homelab-zfs-scrub.sh",
        ZFS_POOLS_BLOCK=shell_array_block("ZFS_POOLS", pools),
    )
    render_file(
        templates_dir / "zfs-scrub.service",
        build_dir / "zfs-scrub.service",
    )


def _write_zfs_env(
    build_dir: Path,
    settings: _HostSettings,
    *,
    pools: list[str],
    snapshot_plans: Any,
) -> None:
    """The env install.py parses: what to enable, and what to freeze."""
    write_env_file(
        build_dir / "env",
        {
            "HOMELAB_STATE_DIR": settings.homelab_state_dir,
            "PAUSED": _flag(settings.paused),
            "ENABLE_ZFS_SNAPSHOTS": _flag(snapshot_plans and settings.manage_snapshots),
            "ENABLE_ZFS_SCRUB": _flag(pools and settings.manage_scrub),
        },
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    module_dir = root / "zfs-automation"
    templates_dir = module_dir / "templates"

    settings = _read_host_settings(registry, host)
    pools = resolve_pools(registry, host)
    snapshot_plans = normalize_snapshot_plans(registry, host)

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    copy_files(module_dir / "configs", build_dir, STATIC_CONFIG_FILES)
    _write_snapshot_artifacts(templates_dir, build_dir, snapshot_plans, settings.snapshot_schedule)
    _write_scrub_artifacts(templates_dir, build_dir, pools)
    _write_zfs_env(build_dir, settings, pools=pools, snapshot_plans=snapshot_plans)

    artifacts = HostArtifacts(build_dir=build_dir, file_specs=BASE_FILE_SPECS)
    write_file_map(build_dir, artifacts)
    return artifacts
