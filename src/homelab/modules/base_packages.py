from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession
from ..hosts import default_registry
from ..module_support import normalize_string_list, simple_root_installer_deploy

FEATURE = "base-packages"
REMOTE_ROOT = "/tmp/homelab-base-packages"

# Baseline packages every apt-managed host gets. Kept small and boring on
# purpose: these are the tools the agent and the operator expect to exist on any
# host before doing anything else.
#
# mbuffer is load-bearing beyond convenience — zfs-automation's replication jobs
# pipe through it — which is why base-packages runs first in MODULE_ORDER.
# ripgrep is the standard content-search tool; without it, searches on a host
# silently fall back to recursive grep, which is exactly what the scan-boundary
# rules forbid on the storage hosts.
BASE_PACKAGES = (
    "mbuffer",
    "vim",
    "mc",
    "ripgrep",
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    def env_for_host(host: str) -> dict[str, str]:
        return {"BASE_PACKAGES": " ".join(packages_for_host(root, host))}

    def dry_run_details(host: str) -> list[str]:
        packages = " ".join(packages_for_host(root, host))
        return [f"[DRY-RUN] Would ensure packages installed on {host}: {packages}"]

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


def packages_for_host(root: Path, host: str) -> list[str]:
    """Baseline packages plus any host-specific `base-packages.extra` entries.

    The baseline is defined once here and passed down to the installer, so the
    package set is never re-derived in bash.
    """
    registry = default_registry(root)
    extra = normalize_string_list(
        registry.get(host, f"{FEATURE}.extra", None),
        f"{FEATURE}.extra must be a list of package names for {host}",
    )
    packages = list(BASE_PACKAGES)
    for package in extra:
        if package not in packages:
            packages.append(package)
    return packages
