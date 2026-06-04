from __future__ import annotations

from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-docker"

TEMPLATE_FILES = [
    "homelab-docker-update.service",
    "homelab-docker-update.timer",
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
    dependency_units: tuple[str, ...] = ()

    templates_dir = root / "docker" / "templates"
    build_dir = root / "docker" / "build" / host
    prepare_build_dir(build_dir)

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

    write_env_file(
        build_dir / "env",
        {
            "ENABLE_DOCKER_UPDATE_TIMER": update_timer_enabled,
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
