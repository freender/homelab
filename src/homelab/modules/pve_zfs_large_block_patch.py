from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession
from ..module_support import simple_root_installer_deploy

FEATURE = "pve-zfs-large-block-patch"
REMOTE_ROOT = "/tmp/homelab-pve-zfs-large-block-patch"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return simple_root_installer_deploy(
        root,
        requested_host,
        dry_run,
        force,
        session,
        feature=FEATURE,
        remote_root=REMOTE_ROOT,
    )
