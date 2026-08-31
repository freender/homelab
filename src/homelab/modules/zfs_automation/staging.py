"""Per-host build + deploy: render the file map, stage secrets, run install.sh.

This is the module's `deploy_host`/`build_host_artifacts` — it turns the typed
config objects from `.normalize`/`.replication`/`.access` and the rendered
scripts from `.render` into an actual build directory and remote deploy.
"""

from __future__ import annotations

from pathlib import Path

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


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))

    module_dir = root / "zfs-automation"
    artifacts = build_host_artifacts(root, host)
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    secret_context = (
        tmpfs_secret_stage("homelab-zfs-automation.")
        if artifacts.secret_file_specs and not dry_run
        else None
    )

    if secret_context is None:
        print_sub("Comparing with remote configs...")
        diff_pairs = [
            (artifacts.build_dir / spec.build_name, resolve_remote_path(spec))
            for spec in artifacts.file_specs
        ]
        for message in diff_many(connection, diff_pairs):
            print_sub(message)

        if dry_run:
            if feature_paused(registry, host, "zfs-automation"):
                print_sub(
                    f"[DRY-RUN] Would pause zfs-automation on {host} "
                    "(stop and disable snapshot, scrub, and all replication timers)"
                )
            else:
                paused_jobs = [
                    job.name
                    for job in normalize_replication_config(registry, host)
                    if job.paused
                ]
                for job_name in paused_jobs:
                    print_sub(
                        f"[DRY-RUN] Would pause replication job '{job_name}' on {host} "
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
            return

        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            [
                (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
                (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
            ],
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib"),
        )
        return

    with secret_context as secret_dir:
        secret_paths = stage_secret_files(root, secret_dir, artifacts.secret_file_specs)
        print_sub("Comparing with remote configs...")
        diff_pairs = [
            (artifacts.build_dir / spec.build_name, resolve_remote_path(spec))
            for spec in artifacts.file_specs
        ]
        diff_pairs.extend(
            (secret_paths[spec.build_name], spec.remote_path)
            for spec in artifacts.secret_file_specs
        )
        for message in diff_many(connection, diff_pairs):
            print_sub(message)

        upload_paths = [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ]
        upload_paths.extend(
            (secret_paths[spec.build_name], f"{REMOTE_ROOT}/build/{host}/{spec.build_name}")
            for spec in artifacts.secret_file_specs
        )
        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            upload_paths,
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

    homelab_state_dir = str(registry.get(host, "config.homelab_state_dir", "/var/lib/homelab"))
    snapshot_schedule = str(
        registry.get(host, "zfs-automation.snapshot_schedule", "*-*-* 00:00:00")
    )
    manage_snapshots = normalize_bool(
        registry.get(host, "zfs-automation.manage_snapshots", None),
        True,
        f"zfs-automation.manage_snapshots must be true or false for {host}",
    )
    replication_recovery_start_failed = normalize_bool(
        registry.get(host, "zfs-automation.replication_recovery.start_failed", None),
        False,
        f"zfs-automation.replication_recovery.start_failed must be true or false for {host}",
    )
    manage_scrub = normalize_bool(
        registry.get(host, "zfs-automation.manage_scrub", None),
        True,
        f"zfs-automation.manage_scrub must be true or false for {host}",
    )
    # `paused: true` stops and disables ALL managed zfs timers (snapshots, scrub,
    # and every replication job) while keeping the module deployed; distinct
    # from `deploy: false`, which skips the host entirely. This is a single
    # host-wide freeze switch that overrides the per-area manage_* flags.
    paused = feature_paused(registry, host, "zfs-automation")
    pools = resolve_pools(registry, host)
    snapshot_plans = normalize_snapshot_plans(registry, host)
    replication_jobs = normalize_replication_config(
        registry,
        host,
    )
    known_host_refresh = normalize_known_host_refresh(registry, host)
    push_target_access = normalize_push_target_access(registry, host)
    source_private_keys = normalize_source_private_keys(registry, host)

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
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

    file_specs = list(BASE_FILE_SPECS)
    if known_host_refresh:
        (build_dir / "homelab-zfs-refresh-known-hosts.sh").write_text(
            build_known_host_refresh_script(known_host_refresh),
            encoding="utf-8",
        )
        file_specs.append(
            FileSpec(
                "homelab-zfs-refresh-known-hosts.sh",
                "/usr/local/sbin/homelab-zfs-refresh-known-hosts",
                mode="755",
            )
        )
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
        file_specs.extend(
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

    if push_target_access is not None:
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
        file_specs.extend(
            [
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
        )

    secret_file_specs = tuple(
        SecretFileSpec(
            f"source-private-key-{index}",
            private_key.path,
            private_key.secret,
        )
        for index, private_key in enumerate(source_private_keys)
    )

    render_file(
        templates_dir / "homelab-zfs-scrub.sh",
        build_dir / "homelab-zfs-scrub.sh",
        ZFS_POOLS_BLOCK=shell_array_block("ZFS_POOLS", pools),
    )
    render_file(
        templates_dir / "zfs-scrub.service",
        build_dir / "zfs-scrub.service",
    )

    paused_replication_timers = " ".join(
        f"homelab-zfs-replication-{job.name}.timer"
        for job in replication_jobs
        if job.paused
    )

    write_env_file(
        build_dir / "env",
        {
            "HOMELAB_STATE_DIR": homelab_state_dir,
            "PAUSED": "true" if paused else "false",
            "PAUSED_REPLICATION_TIMERS": paused_replication_timers,
            "ENABLE_ZFS_SNAPSHOTS": "true" if snapshot_plans and manage_snapshots else "false",
            "ENABLE_ZFS_REPLICATION": (
                "true" if replication_jobs else "false"
            ),
            "ZFS_REPLICATION_RECOVERY_START_FAILED": (
                "true" if replication_recovery_start_failed else "false"
            ),
            "ENABLE_ZFS_SCRUB": "true" if pools and manage_scrub else "false",
            # These retained cleanup inputs remove access artifacts created by old releases.
            "ENABLE_ZFS_PULL_SOURCE": "false",
            "ZFS_PULL_SOURCE_USER": "zfs-pull",
            "ZFS_PULL_SOURCE_HOME": "/var/lib/homelab-zfs-pull",
            "ENABLE_ZFS_PUSH_TARGET": "true" if push_target_access is not None else "false",
            "ZFS_PUSH_TARGET_USER": push_target_access.user if push_target_access else "zfs-push",
            "ZFS_PUSH_TARGET_HOME": "/var/lib/homelab-zfs-push",
        },
    )

    artifacts = HostArtifacts(
        build_dir=build_dir,
        file_specs=tuple(file_specs),
        secret_file_specs=secret_file_specs,
    )
    write_file_map(build_dir, artifacts)
    return artifacts
