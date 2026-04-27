from __future__ import annotations

from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-docker"

TEMPLATE_FILES = [
    "homelab-docker-start.service",
    "homelab-docker-start.timer",
    "homelab-docker-update.service",
    "homelab-docker-update.timer",
    "syncthing-unpause.service",
    "syncthing-unpause.timer",
    "syncthing-pause.service",
    "syncthing-pause.timer",
]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="docker")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping docker (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "docker" / "templates"
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_user = str(registry.get(host, "config.user"))
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    update_schedule = str(registry.get(host, "docker.update_schedule", "")).strip()
    update_timer_enabled = "true" if update_schedule else "false"
    run_on_boot = str(registry.get(host, "docker.run_on_boot", "false")).lower()
    start_schedule = str(registry.get(host, "docker.start_schedule", "")).strip()
    timer_enabled = "true" if start_schedule else "false"
    docker_start_service_enabled = run_on_boot == "true" or timer_enabled == "true"
    syncthing_unpause_schedule = str(
        registry.get(host, "docker.syncthing_unpause_schedule", "")
    ).strip()
    syncthing_pause_schedule = str(
        registry.get(host, "docker.syncthing_pause_schedule", "")
    ).strip()
    syncthing_timer_enabled = (
        "true" if syncthing_unpause_schedule and syncthing_pause_schedule else "false"
    )
    dependency_units = docker_dependency_units(registry, host)

    templates_dir = root / "docker" / "templates"
    build_dir = root / "docker" / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        templates_dir / "homelab-docker-start.service",
        build_dir / "homelab-docker-start.service",
        DOCKER_DEPENDENCY_UNITS=" ".join(dependency_units),
    )
    if start_schedule:
        render_file(
            templates_dir / "homelab-docker-start.timer",
            build_dir / "homelab-docker-start.timer",
            DOCKER_START_SCHEDULE=start_schedule,
        )
    if update_timer_enabled == "true":
        render_file(
            templates_dir / "homelab-docker-update.service",
            build_dir / "homelab-docker-update.service",
            DOCKER_DEPENDENCY_UNITS=" ".join(dependency_units),
        )
        render_file(
            templates_dir / "homelab-docker-update.timer",
            build_dir / "homelab-docker-update.timer",
            DOCKER_UPDATE_SCHEDULE=update_schedule,
        )

    if syncthing_unpause_schedule and syncthing_pause_schedule:
        render_file(
            templates_dir / "syncthing-unpause.service",
            build_dir / "syncthing-unpause.service",
        )
        render_file(
            templates_dir / "syncthing-unpause.timer",
            build_dir / "syncthing-unpause.timer",
            SYNCTHING_UNPAUSE_SCHEDULE=syncthing_unpause_schedule,
        )
        render_file(
            templates_dir / "syncthing-pause.service",
            build_dir / "syncthing-pause.service",
        )
        render_file(
            templates_dir / "syncthing-pause.timer",
            build_dir / "syncthing-pause.timer",
            SYNCTHING_PAUSE_SCHEDULE=syncthing_pause_schedule,
        )

    write_env_file(
        build_dir / "env",
        {
            "ENABLE_DOCKER_UPDATE_TIMER": update_timer_enabled,
            "RUN_DOCKER_START_ON_BOOT": run_on_boot,
            "ENABLE_DOCKER_START_TIMER": timer_enabled,
            "ENABLE_SYNCTHING_TIMERS": syncthing_timer_enabled,
        },
    )

    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub("Comparing with remote scripts...")
    diff_pairs = [
        (root / "docker" / "scripts" / "start.sh", "/mnt/cache/appdata/start.sh"),
        (root / "docker" / "scripts" / "rm.sh", "/mnt/cache/appdata/rm.sh"),
        (
            root / "docker" / "scripts" / "docker-common.sh",
            "/mnt/cache/appdata/.homelab/docker/docker-common.sh",
        ),
    ]
    if docker_start_service_enabled:
        diff_pairs.append(
            (
                build_dir / "homelab-docker-start.service",
                "/etc/systemd/system/homelab-docker-start.service",
            )
        )
    if timer_enabled == "true":
        diff_pairs += [
            (
                build_dir / "homelab-docker-start.timer",
                "/etc/systemd/system/homelab-docker-start.timer",
            ),
        ]
    if update_timer_enabled == "true":
        diff_pairs += [
            (
                build_dir / "homelab-docker-update.service",
                "/etc/systemd/system/homelab-docker-update.service",
            ),
            (
                build_dir / "homelab-docker-update.timer",
                "/etc/systemd/system/homelab-docker-update.timer",
            ),
        ]
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if syncthing_timer_enabled == "true":
        diff_pairs += [
            (
                build_dir / "syncthing-unpause.service",
                "/etc/systemd/system/syncthing-unpause.service",
            ),
            (
                build_dir / "syncthing-unpause.timer",
                "/etc/systemd/system/syncthing-unpause.timer",
            ),
            (
                build_dir / "syncthing-pause.service",
                "/etc/systemd/system/syncthing-pause.service",
            ),
            (
                build_dir / "syncthing-pause.timer",
                "/etc/systemd/system/syncthing-pause.timer",
            ),
        ]

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "docker" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="bash",
        remote_subdirs=("build", "lib"),
    )


def docker_dependency_units(registry, host: str) -> tuple[str, ...]:
    if host not in registry.list_hosts(feature="media-pool"):
        return ()
    if str(registry.get(host, "media-pool.enabled", "true")).lower() != "true":
        return ()
    return (
        "homelab-media-pool.service",
        "homelab-media-pool-hdd-only.service",
    )
