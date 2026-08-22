"""zfs-automation: snapshots (sanoid), replication (syncoid), and scrub timers.

Split into a package because the single-file module had grown to ~2,300 lines,
21% of all Python in this repo, with the least dedicated test coverage of any
module here. The submodules form a small DAG with no cycles:

    types        - dataclasses only, no deps on siblings
    normalize    - generic hosts.conf -> typed-object validation, built on types
    replication  - replication-job expansion, built on normalize
    access       - pull/push send-only access + pool list, built on normalize/replication
    render       - generated bash (sanoid/snapshot/replication scripts), built on types/normalize
    staging      - per-host build + deploy, built on all of the above

`deploy()`/`validate()` here are the module's only public entrypoint, matching
every other module's shape (see the deploy-module skill's "Python module
shape"). `normalize_replication_config` is re-exported for
`tests/test_zfs_replication_pause.py`, which predates this split.
"""

from __future__ import annotations

from pathlib import Path

from ... import op_secrets
from ...deploy import DeploySession
from ...hosts import default_registry
from ...module_support import run_module_deploy, validate_secret_reference
from .access import normalize_pull_source_access, resolve_pools
from .normalize import (
    normalize_bool,
    normalize_known_host_refresh,
    normalize_snapshot_plans,
    normalize_source_private_keys,
)
from .replication import normalize_replication_config
from .staging import deploy_host
from .types import STATIC_CONFIG_FILES, TEMPLATE_FILES

__all__ = ["deploy", "validate", "normalize_replication_config"]


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
        normalize_replication_config(registry, host)
        normalize_replication_config(registry, host, include_disabled=True)
        normalize_known_host_refresh(registry, host)
        normalize_bool(
            registry.get(host, "zfs-automation.replication_recovery.start_failed", None),
            False,
            f"zfs-automation.replication_recovery.start_failed must be true or false for {host}",
        )
        normalize_pull_source_access(registry, host)
        normalize_pull_source_access(registry, host, include_disabled=True)
        for private_key in normalize_source_private_keys(registry, host):
            try:
                validate_secret_reference(root, private_key.secret)
            except op_secrets.OpSecretsError as exc:
                raise ValueError(f"{host}: {exc}") from exc
        pools = resolve_pools(registry, host)
        if not pools:
            raise ValueError(f"zfs-automation requires at least one managed pool for {host}")


