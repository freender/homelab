from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession, force_env, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action
from ..ssh import HostConnection

REMOTE_ROOT = "/tmp/homelab-pve-zfs-large-block-patch"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-zfs-large-block-patch")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-zfs-large-block-patch (not applicable to {requested_host})")
        return 0
    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    installer = root / "pve-zfs-large-block-patch" / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"Missing installer: {installer}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )
    if dry_run:
        print_action(f"[DRY-RUN] Would deploy pve-zfs-large-block-patch to {host}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (root / "pve-zfs-large-block-patch" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("lib",),
    )
