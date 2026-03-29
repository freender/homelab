from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-docker"


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

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        user = str(registry.get(host, "docker.user"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    owner = str(registry.get(host, "docker.owner", user))
    group = str(registry.get(host, "docker.group", owner))
    backup_enabled = str(registry.get(host, "docker.backup", "false")).lower()

    build_dir = root / "docker" / "build" / host
    prepare_build_dir(build_dir)
    write_env_file(
        build_dir / "env",
        {
            "DOCKER_USER": user,
            "DOCKER_OWNER": owner,
            "DOCKER_GROUP": group,
            "DOCKER_BACKUP": backup_enabled,
        },
    )

    connection = HostConnection(host)
    print_sub("Comparing with remote scripts...")
    for message in diff_many(connection, [
        (root / "docker" / "scripts" / "start.sh", "/mnt/cache/appdata/start.sh"),
        (root / "docker" / "scripts" / "rm.sh", "/mnt/cache/appdata/rm.sh"),
        (
            root / "docker" / "scripts" / "docker-common.sh",
            "/mnt/cache/appdata/scripts/docker-common.sh",
        ),
    ]):
        print_sub(message)

    if backup_enabled == "true":
        _, message = connection.remote_diff(
            root / "docker" / "scripts" / "backup.sh",
            "/mnt/cache/appdata/scripts/backup.sh",
        )
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
        interpreter="bash",
        remote_subdirs=("build", "lib"),
    )
