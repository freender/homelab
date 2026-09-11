"""ZFS receive access normalization and managed-pool resolution."""

from __future__ import annotations

from .normalize import (
    dataset_pool,
    is_remote_dataset,
    normalize_bool,
    normalize_snapshot_plans,
    normalize_string_list,
    parse_migratable_lxc_group_ref,
    require_safe_authorized_key_option,
    require_string,
)
from .replication import normalize_replication_config
from .types import ZfsPusher, ZfsPushTargetAccess

TEMPLATE_KEYS = {"template", "push_target_access_template"}
PUSHER_KEY_PREFIXES = ("ssh-ed25519 ", "sk-ssh-ed25519@openssh.com ")


def expand_access_template(registry, host: str, config: dict) -> dict:
    """Merge a `template:` reference's defaults under the host's own overrides.

    The reference names another host's `push_target_access_templates` entry, so the
    lookup is against that host's config, not `host`'s.
    """
    template_ref = config.get("template", config.get("push_target_access_template"))
    if template_ref is None:
        return config

    source_host, template_name = parse_migratable_lxc_group_ref(
        template_ref,
        host,
        "push_target_access template",
    )
    templates = registry.get(source_host, "zfs-automation.push_target_access_templates", None)
    if not isinstance(templates, dict):
        raise ValueError(
            f"zfs-automation.push_target_access_templates must be a dict for {source_host}"
        )
    template = templates.get(template_name)
    if not isinstance(template, dict):
        raise ValueError(
            f"push_target_access template {source_host}:{template_name} not found for {host}"
        )

    expanded = dict(template)
    expanded.update((key, value) for key, value in config.items() if key not in TEMPLATE_KEYS)
    return expanded


def normalize_pusher(pusher_config, index: int, host: str) -> ZfsPusher:
    """One entry of `allowed_pushers`, validated for authorized_keys safety."""
    if not isinstance(pusher_config, dict):
        raise ValueError(f"invalid push target allowed_pusher at index {index} for {host}")
    name = require_safe_authorized_key_option(
        pusher_config.get("name", ""),
        f"pusher name required at index {index} for {host}",
    )
    from_address = require_safe_authorized_key_option(
        pusher_config.get("from", ""),
        f"pusher from address required at index {index} for {host}",
    )
    public_key = require_string(
        pusher_config.get("public_key", ""),
        f"pusher public_key required at index {index} for {host}",
    )
    if not public_key.startswith(PUSHER_KEY_PREFIXES):
        raise ValueError(f"pusher public_key at index {index} for {host} must be ed25519")
    return ZfsPusher(name=name, from_address=from_address, public_key=public_key)


def normalize_pushers(config: dict, host: str) -> list[ZfsPusher]:
    """Every `allowed_pushers` entry; the list is required and may not be empty."""
    pusher_configs = config.get("allowed_pushers", [])
    if not isinstance(pusher_configs, list) or not pusher_configs:
        raise ValueError(
            f"zfs-automation.push_target_access.allowed_pushers must be a non-empty list for {host}"
        )
    return [
        normalize_pusher(pusher_config, index, host)
        for index, pusher_config in enumerate(pusher_configs)
    ]


def normalize_push_target_access(
    registry,
    host: str,
    *,
    include_disabled: bool = False,
) -> ZfsPushTargetAccess | None:
    config = registry.get(host, "zfs-automation.push_target_access", None)
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError(f"zfs-automation.push_target_access must be a mapping for {host}")
    config = expand_access_template(registry, host, config)

    enabled = normalize_bool(
        config.get("enabled"),
        True,
        f"zfs-automation.push_target_access.enabled must be true or false for {host}",
    )
    if not enabled and not include_disabled:
        return None

    user = require_safe_authorized_key_option(
        config.get("user", "zfs-push"),
        f"zfs-automation.push_target_access.user is invalid for {host}",
    )
    datasets = normalize_string_list(
        config.get("datasets", []),
        f"zfs-automation.push_target_access.datasets must be a list for {host}",
    )
    if not datasets:
        raise ValueError(f"zfs-automation.push_target_access.datasets is required for {host}")

    return ZfsPushTargetAccess(
        enabled=enabled,
        user=user,
        datasets=tuple(datasets),
        pushers=tuple(normalize_pushers(config, host)),
    )


def local_replication_datasets(replication_jobs) -> list[str]:
    """Every local dataset a replication job reads from or writes to.

    A plan's `source` is optional (an inherited source is resolved elsewhere), and
    a remote endpoint lives on another host's pools, so neither contributes a pool
    to manage here.
    """
    datasets: list[str] = []
    for job in replication_jobs:
        for plan in job.plans:
            candidates = [plan.source, plan.target] if plan.source else [plan.target]
            datasets.extend(dataset for dataset in candidates if not is_remote_dataset(dataset))
    return datasets


def unique_pools(datasets) -> list[str]:
    """The pool of each dataset, first-seen order preserved and duplicates dropped."""
    pools: list[str] = []
    for dataset in datasets:
        pool = dataset_pool(dataset)
        if pool not in pools:
            pools.append(pool)
    return pools


def resolve_pools(registry, host: str) -> list[str]:
    explicit = registry.get(host, "zfs-automation.pools", None)
    if explicit is not None:
        return normalize_string_list(explicit, f"zfs-automation.pools must be a list for {host}")

    snapshot_plans = normalize_snapshot_plans(registry, host)
    replication_jobs = normalize_replication_config(registry, host)
    pools = unique_pools(
        [
            *(plan.dataset for plan in snapshot_plans),
            *local_replication_datasets(replication_jobs),
        ]
    )
    return pools or ["cache"]
