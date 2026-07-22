from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession
from ..hosts import default_registry
from ..module_support import feature_paused, simple_root_installer_deploy

FEATURE = "pve-upgrade"
REMOTE_ROOT = "/tmp/homelab-pve-upgrade"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    def env_for_host(host: str) -> dict[str, str]:
        return {"PAUSED": "true" if paused(root, host) else "false"}

    def dry_run_details(host: str) -> list[str]:
        if paused(root, host):
            return ["[DRY-RUN] Paused; would skip apt dist-upgrade"]
        return [
            "[DRY-RUN] Would run apt-get update && dist-upgrade "
            f"on {host} (warns only on reboot-required, never reboots)"
        ]

    return simple_root_installer_deploy(
        root,
        requested_host,
        dry_run,
        force,
        session,
        feature=FEATURE,
        remote_root=REMOTE_ROOT,
        env_for_host=env_for_host,
        dry_run_details=dry_run_details,
    )


def paused(root: Path, host: str) -> bool:
    registry = default_registry(root)
    return feature_paused(registry, host, FEATURE)
