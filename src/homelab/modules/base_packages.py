from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession
from ..hosts import default_registry
from ..module_support import (
    normalize_bool,
    normalize_string_list,
    simple_root_installer_deploy,
)

FEATURE = "base-packages"
REMOTE_ROOT = "/tmp/homelab-base-packages"

# Ported to `homelab_install` (freender/homelab-ops#30). The `.py` suffix is what
# makes the staging step upload `homelab_install` and set `PYTHONPATH`, so the two must
# agree; unlike keepalived there is only one call site, because
# `simple_root_installer_deploy` validates the installer path it is given.
INSTALLER = "scripts/install.py"
INTERPRETER = "python3"

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
        return {
            "BASE_PACKAGES": " ".join(packages_for_host(root, host)),
            "BACKPORTS_KERNEL": "true" if backports_kernel(root, host) else "false",
        }

    def dry_run_details(host: str) -> list[str]:
        packages = " ".join(packages_for_host(root, host))
        details = [f"[DRY-RUN] Would ensure packages installed on {host}: {packages}"]
        if backports_kernel(root, host):
            details.append(f"[DRY-RUN] Would move {host}'s kernel to <codename>-backports")
        return details

    return simple_root_installer_deploy(
        root,
        requested_host,
        dry_run,
        force,
        session,
        feature=FEATURE,
        remote_root=REMOTE_ROOT,
        installer=INSTALLER,
        interpreter=INTERPRETER,
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


def backports_kernel(root: Path, host: str) -> bool:
    """Whether the host runs Debian's backports kernel instead of the stable one.

    Only for VMs, which boot their own kernel. PVE nodes run Proxmox's kernel and
    LXCs share their node's, so the flag means nothing there.
    """
    registry = default_registry(root)
    return normalize_bool(
        registry.get(host, f"{FEATURE}.backports_kernel", None),
        False,
        f"{FEATURE}.backports_kernel must be true or false for {host}",
    )
