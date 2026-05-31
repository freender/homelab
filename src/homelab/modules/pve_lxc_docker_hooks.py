from __future__ import annotations

import shutil
from pathlib import Path

from .. import op_secrets
from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub, print_warn
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-lxc-docker-hooks"
FEATURE = "pve-lxc-docker-hooks"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature=FEATURE)
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping {FEATURE} (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    installer = root / "pve-lxc-docker-hooks" / "scripts" / "install.sh"
    hook = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-sync-hook.sh"
    monitor = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.sh"
    monitor_svc = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.service"
    monitor_timer = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.timer"
    bbolt = root / "pve-lxc-docker-hooks" / "configs" / "bbolt"
    for path in (installer, hook, monitor, monitor_svc, monitor_timer, bbolt):
        if not path.is_file():
            raise ValueError(f"missing required file: {path}")

    registry = default_registry(root)
    for host in hosts:
        vmids = registry.get(host, f"{FEATURE}.vmids", [])
        if not isinstance(vmids, list) or not vmids:
            raise ValueError(f"{FEATURE}.vmids must be a non-empty list for {host}")
        for vmid in vmids:
            if not isinstance(vmid, int):
                raise ValueError(f"{FEATURE}.vmids entries must be integers for {host}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )
    vmids = registry.get(host, f"{FEATURE}.vmids")

    build_dir = root / "pve-lxc-docker-hooks" / "build" / host
    prepare_build_dir(build_dir)
    write_env_file(
        build_dir / "env",
        {
            "DOCKER_LXC_VMIDS": " ".join(str(vmid) for vmid in vmids),
        },
    )

    telegram_src: Path | None = None
    try:
        telegram_src = op_secrets.secret_file(root, "telegram")
        shutil.copy2(telegram_src, build_dir / "telegram.env")
    except op_secrets.OpSecretsError:
        print_warn(f"{host}: telegram secret unavailable; Telegram notifications will be disabled")

    diff_pairs = [
        (
            root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-sync-hook.sh",
            "/var/lib/vz/snippets/homelab-docker-bbolt-sync-hook.sh",
        ),
        (
            root / "pve-lxc-docker-hooks" / "configs" / "bbolt",
            "/usr/local/bin/bbolt",
        ),
        (
            root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.sh",
            "/usr/local/sbin/homelab-docker-bbolt-monitor.sh",
        ),
        (
            root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.service",
            "/etc/systemd/system/homelab-docker-bbolt-monitor.service",
        ),
        (
            root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.timer",
            "/etc/systemd/system/homelab-docker-bbolt-monitor.timer",
        ),
    ]
    if telegram_src is not None:
        diff_pairs.append((telegram_src, "/etc/homelab/telegram.env"))
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if dry_run:
        print_action(f"[DRY-RUN] Would deploy {FEATURE} to {host}:{REMOTE_ROOT}/")
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
            (root / "pve-lxc-docker-hooks" / "scripts", f"{REMOTE_ROOT}/scripts"),
            (root / "pve-lxc-docker-hooks" / "configs", f"{REMOTE_ROOT}/configs"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="bash",
        remote_subdirs=("build", "lib"),
    )
