"""Per-host build + deploy: render the file map, stage secrets, run install.sh.

This is the module's `deploy_host`/`build_host_artifacts` — it turns the typed
config objects from `.normalize`/`.replication`/`.access` and the rendered
scripts from `.render` into an actual build directory and remote deploy.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...build import copy_file, copy_files, render_file, write_env_file
from ...deploy import force_env, prepare_build_dir, stage_and_run_remote_installer
from ...hosts import default_registry
from ...module_support import feature_paused, tmpfs_secret_stage
from ...output import print_sub
from ...ssh import HostConnection, build_files, diff_many
from .access import normalize_push_target_access, resolve_pools
from .normalize import (
    normalize_bool,
    normalize_known_host_refresh,
    normalize_snapshot_plans,
    normalize_source_private_keys,
    rendered_private_key,
)
from .render import (
    build_known_host_refresh_script,
    build_replication_script,
    build_sanoid_config,
    build_snapshot_script,
    build_zfs_push_target_authorized_keys,
    shell_array_block,
)
from .replication import normalize_replication_config
from .types import (
    BASE_FILE_SPECS,
    REMOTE_ROOT,
    STATIC_CONFIG_FILES,
    FileSpec,
    HostArtifacts,
    SecretFileSpec,
)


def resolve_remote_path(spec: FileSpec) -> str:
    return spec.remote_path


def write_file_map(build_dir: Path, artifacts: HostArtifacts) -> None:
    lines = []
    for spec in artifacts.file_specs:
        lines.append(f"{spec.build_name}|{resolve_remote_path(spec)}|{spec.mode}")
    for spec in artifacts.secret_file_specs:
        lines.append(f"{spec.build_name}|{spec.remote_path}|{spec.mode}")
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_secret_files(
    root: Path,
    secret_dir: Path,
    secret_specs: tuple[SecretFileSpec, ...],
) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    for spec in secret_specs:
        path = secret_dir / spec.build_name
        path.write_text(rendered_private_key(root, spec.secret), encoding="utf-8")
        path.chmod(0o600)
        staged[spec.build_name] = path
    return staged


def diff_pairs_for(
    artifacts: HostArtifacts,
    secret_paths: dict[str, Path],
) -> list[tuple[Path, str]]:
    """Local/remote pairs to diff: rendered build files, then any staged secrets.

    Secrets appear only when they were actually staged (a real deploy). On a dry
    run they are not rendered at all, so there is nothing to compare against.
    """
    pairs = [
        (artifacts.build_dir / spec.build_name, resolve_remote_path(spec))
        for spec in artifacts.file_specs
    ]
    pairs.extend(
        (secret_paths[spec.build_name], spec.remote_path)
        for spec in artifacts.secret_file_specs
        if spec.build_name in secret_paths
    )
    return pairs


def upload_paths_for(
    module_dir: Path,
    host: str,
    artifacts: HostArtifacts,
    secret_paths: dict[str, Path],
) -> list[tuple[Path, str]]:
    """The upload set: the build dir, the installer scripts, and staged secrets.

    Secret files are uploaded individually into the remote build dir rather than
    living in the local build dir, which is a persistent mode-0644 directory in
    the repo. They exist only in tmpfs on this side.
    """
    paths = [
        (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]
    paths.extend(
        (secret_paths[spec.build_name], f"{REMOTE_ROOT}/build/{host}/{spec.build_name}")
        for spec in artifacts.secret_file_specs
        if spec.build_name in secret_paths
    )
    return paths


def report_dry_run(registry: Any, host: str, artifacts: HostArtifacts) -> None:
    """Print what a real deploy would do, including which timers it would pause."""
    if feature_paused(registry, host, "zfs-automation"):
        print_sub(
            f"[DRY-RUN] Would pause zfs-automation on {host} "
            "(stop and disable snapshot, scrub, and all replication timers)"
        )
    else:
        for job in normalize_replication_config(registry, host):
            if job.paused:
                print_sub(
                    f"[DRY-RUN] Would pause replication job '{job.name}' on {host} "
                    "(stop and disable its timer; job stays deployed)"
                )
    print_sub(f"[DRY-RUN] Would deploy zfs-automation to {host}")
    print_sub("Build files:")
    for file_name in build_files(artifacts.build_dir):
        print_sub(f"    {file_name}")
    if artifacts.secret_file_specs:
        print_sub("Secret files staged only during real deploy:")
        for spec in artifacts.secret_file_specs:
            print_sub(f"    {spec.build_name}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))

    module_dir = root / "zfs-automation"
    artifacts = build_host_artifacts(root, host)
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    # Secrets are staged in tmpfs for a real deploy only; a dry run neither renders
    # nor uploads them. nullcontext keeps that a single code path -- the two arms
    # previously duplicated the diff, upload, and installer calls between them.
    stage_secrets = bool(artifacts.secret_file_specs) and not dry_run
    secret_context = (
        tmpfs_secret_stage("homelab-zfs-automation.") if stage_secrets else nullcontext()
    )

    with secret_context as secret_dir:
        secret_paths = (
            stage_secret_files(root, secret_dir, artifacts.secret_file_specs)
            if stage_secrets
            else {}
        )

        print_sub("Comparing with remote configs...")
        for message in diff_many(connection, diff_pairs_for(artifacts, secret_paths)):
            print_sub(message)

        if dry_run:
            report_dry_run(registry, host, artifacts)
            return

        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            upload_paths_for(module_dir, host, artifacts, secret_paths),
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib"),
        )


def _flag(value: object) -> str:
    """Render a truthiness check as the "true"/"false" strings install.sh reads."""
    return "true" if value else "false"


@dataclass(frozen=True)
class _HostSettings:
    """The hosts.conf-derived scalars for one host, all normalized in one place."""

    homelab_state_dir: str
    snapshot_schedule: str
    manage_snapshots: bool
    manage_scrub: bool
    replication_recovery_start_failed: bool
    paused: bool


def _read_host_settings(registry: Any, host: str) -> _HostSettings:
    """Read and normalize every scalar knob, so the flags fail here or not at all.

    `paused: true` stops and disables ALL managed zfs timers (snapshots, scrub,
    and every replication job) while keeping the module deployed; distinct from
    `deploy: false`, which skips the host entirely. It is a single host-wide
    freeze switch that overrides the per-area manage_* flags.
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
        replication_recovery_start_failed=normalize_bool(
            registry.get(host, "zfs-automation.replication_recovery.start_failed", None),
            False,
            f"zfs-automation.replication_recovery.start_failed must be true or false for {host}",
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


def _known_host_refresh_specs(build_dir: Path, known_host_refresh: Any) -> list[FileSpec]:
    """Nothing at all unless the host declares hosts to refresh."""
    if not known_host_refresh:
        return []
    (build_dir / "homelab-zfs-refresh-known-hosts.sh").write_text(
        build_known_host_refresh_script(known_host_refresh),
        encoding="utf-8",
    )
    return [
        FileSpec(
            "homelab-zfs-refresh-known-hosts.sh",
            "/usr/local/sbin/homelab-zfs-refresh-known-hosts",
            mode="755",
        )
    ]


def _replication_specs(
    templates_dir: Path, build_dir: Path, replication_jobs: Any
) -> list[FileSpec]:
    """One unit, timer, and syncoid script per replication job."""
    specs: list[FileSpec] = []
    for job in replication_jobs:
        script_name = f"homelab-zfs-replication-{job.name}.sh"
        service_name = f"homelab-zfs-replication-{job.name}.service"
        timer_name = f"homelab-zfs-replication-{job.name}.timer"

        render_file(
            templates_dir / "homelab-zfs-replication.service",
            build_dir / service_name,
            SCRIPT_PATH=f"/usr/local/bin/homelab-zfs-replication-{job.name}",
        )
        render_file(
            templates_dir / "homelab-zfs-replication.timer",
            build_dir / timer_name,
            REPLICATION_SCHEDULE=job.schedule,
        )
        (build_dir / script_name).write_text(
            build_replication_script(
                list(job.plans),
                list(job.syncoid_options),
                job.delete_target_snapshots,
            ),
            encoding="utf-8",
        )
        specs.extend(
            [
                FileSpec(
                    service_name,
                    f"/etc/systemd/system/{service_name}",
                ),
                FileSpec(
                    timer_name,
                    f"/etc/systemd/system/{timer_name}",
                ),
                FileSpec(
                    script_name,
                    f"/usr/local/bin/homelab-zfs-replication-{job.name}",
                    mode="755",
                ),
            ]
        )
    return specs


def _push_target_specs(root: Path, build_dir: Path, push_target_access: Any) -> list[FileSpec]:
    """The receive-only wrapper, its dataset allow-list, and authorized_keys.

    Only for a host that *receives* a push; a pure source gets none of it.
    """
    if push_target_access is None:
        return []
    copy_file(
        root / "zfs-automation" / "templates" / "homelab-zfs-receive-only.sh",
        build_dir / "homelab-zfs-receive-only.sh",
    )
    (build_dir / "zfs-push-datasets.conf").write_text(
        "\n".join(push_target_access.datasets) + "\n",
        encoding="utf-8",
    )
    (build_dir / "zfs-push-authorized-keys").write_text(
        build_zfs_push_target_authorized_keys(push_target_access),
        encoding="utf-8",
    )
    return [
        FileSpec(
            "homelab-zfs-receive-only.sh",
            "/usr/local/sbin/homelab-zfs-receive-only",
            mode="755",
        ),
        FileSpec("zfs-push-datasets.conf", "/etc/homelab/zfs-push-datasets.conf"),
        FileSpec(
            "zfs-push-authorized-keys",
            "/var/lib/homelab-zfs-push/.ssh/authorized_keys",
            mode="600",
        ),
    ]


def _write_zfs_env(
    build_dir: Path,
    settings: _HostSettings,
    *,
    pools: list[str],
    snapshot_plans: Any,
    replication_jobs: Any,
    push_target_access: Any,
) -> None:
    """The env install.sh sources: what to enable, and what to freeze."""
    paused_replication_timers = " ".join(
        f"homelab-zfs-replication-{job.name}.timer"
        for job in replication_jobs
        if job.paused
    )

    write_env_file(
        build_dir / "env",
        {
            "HOMELAB_STATE_DIR": settings.homelab_state_dir,
            "PAUSED": _flag(settings.paused),
            "PAUSED_REPLICATION_TIMERS": paused_replication_timers,
            "ENABLE_ZFS_SNAPSHOTS": _flag(snapshot_plans and settings.manage_snapshots),
            "ENABLE_ZFS_REPLICATION": _flag(replication_jobs),
            "ZFS_REPLICATION_RECOVERY_START_FAILED": _flag(
                settings.replication_recovery_start_failed
            ),
            "ENABLE_ZFS_SCRUB": _flag(pools and settings.manage_scrub),
            # These retained cleanup inputs remove access artifacts created by old releases.
            "ENABLE_ZFS_PULL_SOURCE": "false",
            "ZFS_PULL_SOURCE_USER": "zfs-pull",
            "ZFS_PULL_SOURCE_HOME": "/var/lib/homelab-zfs-pull",
            "ENABLE_ZFS_PUSH_TARGET": _flag(push_target_access is not None),
            "ZFS_PUSH_TARGET_USER": push_target_access.user if push_target_access else "zfs-push",
            "ZFS_PUSH_TARGET_HOME": "/var/lib/homelab-zfs-push",
        },
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    module_dir = root / "zfs-automation"
    templates_dir = module_dir / "templates"

    settings = _read_host_settings(registry, host)
    pools = resolve_pools(registry, host)
    snapshot_plans = normalize_snapshot_plans(registry, host)
    replication_jobs = normalize_replication_config(registry, host)
    known_host_refresh = normalize_known_host_refresh(registry, host)
    push_target_access = normalize_push_target_access(registry, host)
    source_private_keys = normalize_source_private_keys(registry, host)

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    copy_files(module_dir / "configs", build_dir, STATIC_CONFIG_FILES)
    _write_snapshot_artifacts(templates_dir, build_dir, snapshot_plans, settings.snapshot_schedule)
    _write_scrub_artifacts(templates_dir, build_dir, pools)

    # Order matters: file-map.conf is written in this order and install.sh
    # applies it top to bottom.
    file_specs = [
        *BASE_FILE_SPECS,
        *_known_host_refresh_specs(build_dir, known_host_refresh),
        *_replication_specs(templates_dir, build_dir, replication_jobs),
        *_push_target_specs(root, build_dir, push_target_access),
    ]

    _write_zfs_env(
        build_dir,
        settings,
        pools=pools,
        snapshot_plans=snapshot_plans,
        replication_jobs=replication_jobs,
        push_target_access=push_target_access,
    )

    artifacts = HostArtifacts(
        build_dir=build_dir,
        file_specs=tuple(file_specs),
        secret_file_specs=tuple(
            SecretFileSpec(
                f"source-private-key-{index}",
                private_key.path,
                private_key.secret,
            )
            for index, private_key in enumerate(source_private_keys)
        ),
    )
    write_file_map(build_dir, artifacts)
    return artifacts
