"""Config normalization: turn raw hosts.conf values into typed objects.

Covers generic scalar/list validation, snapshot plans, and migratable-LXC
groups.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .types import (
    GUEST_ACTIVE_UNITS,
    MigratableLxcGroup,
    MigratableLxcPlan,
    SnapshotPlan,
)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def require_string(value: object, message: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(message)
    return text


def normalize_string_list(value: object, message: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        raise ValueError(message)
    return [require_string(item, message) for item in value]


def normalize_bool(value: object, default: bool, message: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(message)


def normalize_migratable_lxc_group_name(value: object, host: str) -> str:
    name = require_string(value, f"migratable LXC group name required for {host}")
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"migratable LXC group '{name}' for {host} must use only letters, "
            "numbers, underscores, or hyphens and must start with a letter or number"
        )
    return name


def normalize_positive_int(value: object, message: str) -> int:
    try:
        normalized = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if normalized <= 0:
        raise ValueError(message)
    return normalized


def require_safe_name(value: object, message: str) -> str:
    text = require_string(value, message)
    if any(char in text for char in ['"', "'", ",", " ", "\t", "\n", "\r"]):
        raise ValueError(message)
    return text


def dataset_pool(dataset: str) -> str:
    return dataset.split("/", 1)[0]


def snapshot_plan_from_config(
    plan: dict,
    defaults: dict,
    dataset: str,
    host: str,
) -> SnapshotPlan:
    return SnapshotPlan(
        dataset=dataset,
        hourly=str(plan.get("hourly", defaults.get("hourly", 0))),
        daily=str(plan.get("daily", defaults.get("daily", 7))),
        weekly=str(plan.get("weekly", defaults.get("weekly", 4))),
        monthly=str(plan.get("monthly", defaults.get("monthly", 3))),
        yearly=str(plan.get("yearly", defaults.get("yearly", 0))),
        recursive=normalize_bool(
            plan.get("recursive", defaults.get("recursive")),
            True,
            f"recursive for snapshot plan {dataset} must be true or false for {host}",
        ),
        process_children_only=normalize_bool(
            plan.get("process_children_only", defaults.get("process_children_only")),
            True,
            f"process_children_only for snapshot plan {dataset} must be true or false for {host}",
        ),
        autosnap=normalize_bool(
            plan.get("autosnap", defaults.get("autosnap")),
            True,
            f"autosnap for snapshot plan {dataset} must be true or false for {host}",
        ),
        require_active_guest=(
            normalize_positive_int(
                plan.get("require_active_lxc"),
                "require_active_lxc must be a positive integer for snapshot plan "
                f"{dataset} on {host}",
            )
            if plan.get("require_active_lxc") is not None
            else None
        ),
    )


def normalize_migratable_lxc_groups(
    registry,
    host: str,
) -> dict[str, MigratableLxcGroup]:
    groups_config = registry.get(host, "zfs-automation.migratable_lxc_groups", None)
    if groups_config is None:
        return {}
    if not isinstance(groups_config, dict):
        raise ValueError(f"zfs-automation.migratable_lxc_groups must be a dict for {host}")

    groups: dict[str, MigratableLxcGroup] = {}
    for group_name, group_config in groups_config.items():
        if not isinstance(group_config, dict):
            raise ValueError(f"invalid migratable LXC group '{group_name}' for {host}")
        normalized_group_name = normalize_migratable_lxc_group_name(group_name, host)
        if normalized_group_name in groups:
            raise ValueError(
                f"duplicate migratable LXC group '{normalized_group_name}' for {host}"
            )
        groups[normalized_group_name] = MigratableLxcGroup(
            name=normalized_group_name,
            plans=tuple(
                normalize_migratable_lxc_plans(group_config, normalized_group_name, host)
            ),
        )
    return groups


def normalize_migratable_lxc_plan(
    plan: object, index: int, group_name: str, host: str
) -> MigratableLxcPlan:
    """One plan in a migratable-guest group: a guest name, its VMID, and its dataset.

    `kind` (default `lxc`) picks the unit that marks the guest active locally, so a
    VM's zvols are snapshotted only on the node running it, like a container's.
    """
    where = f"{index} in group '{group_name}' for {host}"
    if not isinstance(plan, dict):
        raise ValueError(f"invalid migratable LXC plan at index {where}")
    kind = str(plan.get("kind", "lxc")).strip()
    if kind not in GUEST_ACTIVE_UNITS:
        raise ValueError(
            f"kind must be one of {', '.join(GUEST_ACTIVE_UNITS)} for migratable LXC plan {where}"
        )
    return MigratableLxcPlan(
        kind=kind,
        name=require_safe_name(
            plan.get("name", f"plan-{index}"),
            f"name must be safe for migratable LXC plan {where}",
        ),
        vmid=normalize_positive_int(
            plan.get("vmid"),
            f"vmid must be a positive integer for migratable LXC plan {where}",
        ),
        dataset=require_string(
            plan.get("dataset", ""),
            f"dataset required for migratable LXC plan {where}",
        ),
    )


def normalize_migratable_lxc_plans(
    group_config: dict, group_name: str, host: str
) -> list[MigratableLxcPlan]:
    """A group's plans, rejecting a repeated name or dataset.

    Both must be unique: a repeated name is a copy-paste slip, and a repeated
    dataset would render the same sanoid section twice.
    """
    explicit_plans = group_config.get("plans", [])
    if not isinstance(explicit_plans, list) or not explicit_plans:
        raise ValueError(
            f"migratable LXC group '{group_name}' plans must be a non-empty list for {host}"
        )

    plans: list[MigratableLxcPlan] = []
    seen_names: set[str] = set()
    seen_datasets: set[str] = set()
    for index, raw_plan in enumerate(explicit_plans):
        plan = normalize_migratable_lxc_plan(raw_plan, index, group_name, host)
        if plan.name in seen_names:
            raise ValueError(
                f"duplicate plan name {plan.name} in migratable LXC group "
                f"'{group_name}' for {host}"
            )
        seen_names.add(plan.name)
        if plan.dataset in seen_datasets:
            raise ValueError(
                f"duplicate dataset {plan.dataset} in migratable LXC group "
                f"'{group_name}' for {host}"
            )
        seen_datasets.add(plan.dataset)
        plans.append(plan)
    return plans


def parse_migratable_lxc_group_ref(value: object, host: str, key: str) -> tuple[str, str]:
    ref = require_string(value, f"{key} must be set for {host}")
    if ":" in ref:
        source_host, group_name = ref.split(":", 1)
        return source_host, group_name
    if "." in ref:
        source_host, group_name = ref.split(".", 1)
        return source_host, group_name
    raise ValueError(f"{key} for {host} must use host:group format")


def resolve_migratable_lxc_group(
    registry,
    value: object,
    host: str,
    key: str,
) -> MigratableLxcGroup:
    source_host, group_name = parse_migratable_lxc_group_ref(value, host, key)
    groups = normalize_migratable_lxc_groups(registry, source_host)
    group = groups.get(group_name)
    if group is None:
        raise ValueError(f"{key} {source_host}:{group_name} not found for {host}")
    return group


def expand_migratable_lxc_snapshot_group(
    registry,
    value: object,
    defaults: dict,
    host: str,
) -> list[SnapshotPlan]:
    group = resolve_migratable_lxc_group(registry, value, host, "migratable_lxc_group")

    return [
        replace(
            snapshot_plan_from_config(
                {"require_active_lxc": plan.vmid},
                defaults,
                plan.dataset,
                host,
            ),
            guest_kind=plan.kind,
        )
        for plan in group.plans
    ]


def resolve_snapshot_sources(registry, host: str) -> list[tuple[object, object]]:
    """The `(defaults, explicit_plans)` pairs: a snapshot_template's, then the host's own.

    Each pair keeps its own defaults, so a template's retention never leaks into a
    host plan or the other way round. A host on a template adds its own
    `snapshot_plans` after the template's, e.g. a prune-only plan for a dataset
    only that host holds.
    """
    own = (
        registry.get(host, "zfs-automation.snapshot_defaults", {}),
        registry.get(host, "zfs-automation.snapshot_plans", None),
    )
    snapshot_template = registry.get(host, "zfs-automation.snapshot_template", None)
    if snapshot_template is None:
        return [own]
    return [resolve_snapshot_template(registry, snapshot_template, host), own]


def resolve_snapshot_template(
    registry, snapshot_template: object, host: str
) -> tuple[object, object]:
    source_host, template_name = parse_migratable_lxc_group_ref(
        snapshot_template,
        host,
        "snapshot_template",
    )
    templates = registry.get(source_host, "zfs-automation.snapshot_templates", None)
    if not isinstance(templates, dict):
        raise ValueError(f"zfs-automation.snapshot_templates must be a dict for {source_host}")
    template = templates.get(template_name)
    if not isinstance(template, dict):
        raise ValueError(f"snapshot_template {source_host}:{template_name} not found for {host}")
    return template.get("snapshot_defaults", {}), template.get("snapshot_plans", None)


def expand_snapshot_plan_entry(registry, plan: object, index: int, defaults: dict, host: str):
    """One `snapshot_plans` entry, which may fan out into a whole LXC group."""
    if not isinstance(plan, dict):
        raise ValueError(f"invalid snapshot plan at index {index} for {host}")
    if "migratable_lxc_group" in plan:
        return expand_migratable_lxc_snapshot_group(
            registry,
            plan.get("migratable_lxc_group"),
            defaults,
            host,
        )
    return [
        snapshot_plan_from_config(
            plan,
            defaults,
            require_string(
                plan.get("dataset", ""),
                f"snapshot plan dataset required for {host}",
            ),
            host,
        )
    ]


def expand_snapshot_source(registry, defaults: object, explicit: object, host: str):
    """One `(defaults, explicit_plans)` pair as plans; `None` when it declares none."""
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError(f"zfs-automation.snapshot_defaults must be a mapping for {host}")
    if explicit is None:
        return None
    if not isinstance(explicit, list):
        raise ValueError(f"zfs-automation.snapshot_plans must be a list for {host}")
    return [
        expanded_plan
        for index, plan in enumerate(explicit)
        for expanded_plan in expand_snapshot_plan_entry(registry, plan, index, defaults, host)
    ]


def normalize_snapshot_plans(registry, host: str) -> list[SnapshotPlan]:
    expanded = [
        expand_snapshot_source(registry, defaults, explicit, host)
        for defaults, explicit in resolve_snapshot_sources(registry, host)
    ]
    if all(source is None for source in expanded):
        if registry.get(host, "zfs-automation.sanoid", None) is not None:
            raise ValueError(
                f"zfs-automation.sanoid is no longer supported for {host}; use snapshot_plans"
            )
        return []

    plans: list[SnapshotPlan] = []
    seen: set[str] = set()
    for expanded_plan in (plan for source in expanded if source for plan in source):
        if expanded_plan.dataset in seen:
            raise ValueError(f"duplicate snapshot plan dataset {expanded_plan.dataset} for {host}")
        seen.add(expanded_plan.dataset)
        plans.append(expanded_plan)
    return plans
