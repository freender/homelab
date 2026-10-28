"""Managed-pool resolution: which pools get scrubbed on a host."""

from __future__ import annotations

from .normalize import dataset_pool, normalize_snapshot_plans, normalize_string_list


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
    pools = unique_pools(plan.dataset for plan in snapshot_plans)
    return pools or ["cache"]
