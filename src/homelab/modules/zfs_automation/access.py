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
    template_ref = config.get("template", config.get("push_target_access_template"))
    if template_ref is not None:
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
        expanded_config = dict(template)
        expanded_config.update(
            (key, value)
            for key, value in config.items()
            if key not in {"template", "push_target_access_template"}
        )
        config = expanded_config

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

    pusher_configs = config.get("allowed_pushers", [])
    if not isinstance(pusher_configs, list) or not pusher_configs:
        raise ValueError(
            f"zfs-automation.push_target_access.allowed_pushers must be a non-empty list"
            f" for {host}"
        )
    pushers: list[ZfsPusher] = []
    for index, pusher_config in enumerate(pusher_configs):
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
        if not public_key.startswith(("ssh-ed25519 ", "sk-ssh-ed25519@openssh.com ")):
            raise ValueError(f"pusher public_key at index {index} for {host} must be ed25519")
        pushers.append(ZfsPusher(name=name, from_address=from_address, public_key=public_key))

    return ZfsPushTargetAccess(
        enabled=enabled,
        user=user,
        datasets=tuple(datasets),
        pushers=tuple(pushers),
    )


def resolve_pools(registry, host: str) -> list[str]:
    explicit = registry.get(host, "zfs-automation.pools", None)
    if explicit is not None:
        return normalize_string_list(explicit, f"zfs-automation.pools must be a list for {host}")

    snapshot_plans = normalize_snapshot_plans(registry, host)
    replication_jobs = normalize_replication_config(registry, host)
    pools: list[str] = []
    local_replication_datasets = [
        dataset
        for job in replication_jobs
        for plan in job.plans
        for dataset in (
            *([plan.source] if plan.source else []),
            plan.target,
        )
        if not is_remote_dataset(dataset)
    ]
    for dataset in [*(plan.dataset for plan in snapshot_plans), *local_replication_datasets]:
        pool = dataset_pool(dataset)
        if pool not in pools:
            pools.append(pool)
    if pools:
        return pools
    return ["cache"]

