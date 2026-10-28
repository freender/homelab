"""zfs-automation: snapshots (sanoid), scrub, and TRIM timers.

The submodules form a small DAG with no cycles:

    types      - dataclasses only, no deps on siblings
    normalize  - generic hosts.conf -> typed-object validation, built on types
    pools      - managed-pool resolution, built on normalize
    render     - generated bash (sanoid config, snapshot script), built on types
    staging    - per-host build + deploy, built on all of the above

`deploy()`/`validate()` here are the module's only public entrypoint, matching
every other module's shape (see the deploy-module skill's "Python module
shape").
"""

from __future__ import annotations

from pathlib import Path

from ...deploy import DeploySession
from ...hosts import default_registry
from ...module_support import run_module_deploy
from .normalize import normalize_snapshot_plans
from .pools import resolve_pools
from .staging import deploy_host
from .types import STATIC_CONFIG_FILES, TEMPLATE_FILES

__all__ = ["deploy", "validate"]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        "zfs-automation",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / "zfs-automation"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    for file_name in STATIC_CONFIG_FILES:
        file_path = config_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required config: {file_path}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        normalize_snapshot_plans(registry, host)
        pools = resolve_pools(registry, host)
        if not pools:
            raise ValueError(f"zfs-automation requires at least one managed pool for {host}")

