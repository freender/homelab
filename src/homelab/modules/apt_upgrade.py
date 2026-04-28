from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection

REMOTE_ROOT = "/tmp/homelab-apt-upgrade"
SERVICE_NAME = "homelab-apt-dist-upgrade.service"
TIMER_NAME = "homelab-apt-dist-upgrade.timer"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="apt-upgrade")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping apt-upgrade (not applicable to {requested_host})")
        return 0

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        host_type = registry.get(host, "config.type")
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type != "ubuntu":
        print_sub(f"Skipping {host}: apt-upgrade supports type ubuntu only")
        return

    autoupgrade = str(registry.get(host, "apt-upgrade.autoupgrade", "false")).lower()
    schedule = str(registry.get(host, "apt-upgrade.schedule", "*-*-* 09:00:00"))

    build_dir = root / "apt-upgrade" / "build" / host
    prepare_build_dir(build_dir)
    restart_docker_stacks = registry.has(host, "docker")
    write_service(build_dir, cleanup=False, restart_docker_stacks=restart_docker_stacks)
    if autoupgrade == "true":
        write_timer(build_dir, schedule)
    write_env(build_dir, autoupgrade=autoupgrade, schedule=schedule)

    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub("Comparing with remote configs...")
    _, message = connection.remote_diff(
        build_dir / "service",
        f"/etc/systemd/system/{SERVICE_NAME}",
    )
    print_sub(message)
    if autoupgrade == "true":
        _, message = connection.remote_diff(
            build_dir / "timer",
            f"/etc/systemd/system/{TIMER_NAME}",
        )
        print_sub(message)

    if dry_run:
        if autoupgrade == "true":
            print_sub(
                f"[DRY-RUN] Would install apt dist-upgrade timer on {host} at {schedule}"
            )
        else:
            print_sub(f"[DRY-RUN] Would run apt dist-upgrade on {host} (on-demand only)")
        return

    stage_and_install(root, build_dir, connection, force=force)


def write_service(build_dir: Path, cleanup: bool, restart_docker_stacks: bool) -> None:
    lines = [
        "[Unit]",
        "Description=Homelab daily apt update and dist-upgrade",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=/usr/bin/apt-get update",
        "ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y dist-upgrade",
    ]
    if cleanup:
        lines.extend(
            [
                (
                    "ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive "
                    "/usr/bin/apt-get -y autoremove"
                ),
                "ExecStart=/usr/bin/apt-get -y autoclean",
            ]
        )
    if restart_docker_stacks:
        lines.append(
            "ExecStartPost=/bin/bash -lc 'if [[ -x /mnt/cache/appdata/start.sh ]]; "
            "then sleep 90; /mnt/cache/appdata/start.sh --no-pull || "
            "{ sleep 60; /mnt/cache/appdata/start.sh --no-pull; }; fi'"
        )
    (build_dir / "service").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_timer(build_dir: Path, schedule: str) -> None:
    content = "\n".join(
        [
            "[Unit]",
            "Description=Run homelab daily apt update and dist-upgrade",
            "",
            "[Timer]",
            f"OnCalendar={schedule}",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    (build_dir / "timer").write_text(content, encoding="utf-8")


def write_env(build_dir: Path, autoupgrade: str, schedule: str) -> None:
    write_env_file(
        build_dir / "env",
        {
            "CLEANUP": "false",
            "AUTOUPGRADE": autoupgrade,
            "SCHEDULE": schedule,
        },
    )


def stage_and_install(root: Path, build_dir: Path, connection: HostConnection, force: bool) -> None:
    upload_paths: list[tuple[Path, str]] = [
        (root / "apt-upgrade" / "scripts", f"{REMOTE_ROOT}/scripts")
    ]
    for file_name in ["service", "env", "timer"]:
        file_path = build_dir / file_name
        if file_path.is_file():
            upload_paths.append((file_path, f"{REMOTE_ROOT}/build/{file_name}"))

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        upload_paths,
        "scripts/install.sh",
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib", "scripts"),
    )
