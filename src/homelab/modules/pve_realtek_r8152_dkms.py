from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import require_text
from ..output import print_action, print_sub
from ..ssh import HostConnection

FEATURE = "pve-realtek-r8152-dkms"
REMOTE_ROOT = "/tmp/homelab-pve-realtek-r8152-dkms"
DEFAULT_REPO_URL = "https://github.com/wget/realtek-r8152-linux.git"
DEFAULT_REPO_REF = "master"
DEFAULT_DRIVER_VERSION = "2.21.4"


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
    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    installer = root / FEATURE / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"Missing installer: {installer}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    del force  # Installer refreshes source and reapplies DKMS idempotently.
    registry = default_registry(root)
    repo_url = require_text(
        registry.get(host, f"{FEATURE}.repo_url", DEFAULT_REPO_URL),
        "repo_url is required",
    )
    repo_ref = require_text(
        registry.get(host, f"{FEATURE}.repo_ref", DEFAULT_REPO_REF),
        "repo_ref is required",
    )
    driver_version = require_text(
        registry.get(host, f"{FEATURE}.driver_version", DEFAULT_DRIVER_VERSION),
        "driver_version is required",
    )

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )
    if dry_run:
        print_action(f"[DRY-RUN] Would deploy {FEATURE} to {host}")
        print_sub(f"Repo: {repo_url}@{repo_ref}")
        print_sub(f"Driver version: {driver_version}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (root / FEATURE / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env={
            "REPO_URL": repo_url,
            "REPO_REF": repo_ref,
            "DRV_VERSION": driver_version,
        },
        require_root=True,
        remote_subdirs=("lib",),
    )
