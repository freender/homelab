from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession
from ..hosts import default_registry
from ..module_support import require_text, simple_root_installer_deploy

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
    def env_for_host(host: str) -> dict[str, str]:
        repo_url, repo_ref, driver_version = driver_config(root, host)
        return {
            "REPO_URL": repo_url,
            "REPO_REF": repo_ref,
            "DRV_VERSION": driver_version,
        }

    def dry_run_details(host: str) -> list[str]:
        repo_url, repo_ref, driver_version = driver_config(root, host)
        return [
            f"Repo: {repo_url}@{repo_ref}",
            f"Driver version: {driver_version}",
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


def driver_config(root: Path, host: str) -> tuple[str, str, str]:
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
    return repo_url, repo_ref, driver_version
