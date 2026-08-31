"""Config normalization: turn raw hosts.conf values into typed objects.

Covers generic scalar/list validation, snapshot plans, and migratable-LXC
groups. Replication-job and push-access normalization live in sibling
`.replication` / `.access` modules since they build on top of what's here.
"""

from __future__ import annotations

import re
from pathlib import Path

from ... import op_secrets
from .types import (
    KnownHostRefresh,
    MigratableLxcGroup,
    MigratableLxcPlan,
    ReplicationPlan,
    SnapshotPlan,
    SourcePrivateKey,
)

REPLICATION_JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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


def normalize_source_private_keys(registry, host: str) -> tuple[SourcePrivateKey, ...]:
    raw = registry.get(host, "zfs-automation.source_private_keys", [])
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"zfs-automation.source_private_keys must be a list for {host}")

    keys: list[SourcePrivateKey] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"zfs-automation.source_private_keys[{index}] must be a mapping for {host}"
            )
        secret = require_safe_authorized_key_option(
            item.get("secret", ""),
            f"zfs-automation.source_private_keys[{index}].secret must be safe for {host}",
        )
        path = require_string(
            item.get("path", ""),
            f"zfs-automation.source_private_keys[{index}].path required for {host}",
        )
        if not path.startswith("/root/.ssh/") or path.endswith("/"):
            raise ValueError(
                f"zfs-automation.source_private_keys[{index}].path must be under "
                f"/root/.ssh/ for {host}"
            )
        if any(char in path for char in ["\0", "\n", "\r"]):
            raise ValueError(f"zfs-automation.source_private_keys[{index}].path invalid for {host}")
        if path in seen_paths:
            raise ValueError(f"duplicate source private key path {path} for {host}")
        seen_paths.add(path)
        keys.append(SourcePrivateKey(secret=secret, path=path))
    return tuple(keys)


def normalize_known_host_refresh(registry, host: str) -> tuple[KnownHostRefresh, ...]:
    raw = registry.get(host, "zfs-automation.known_host_refresh", [])
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"zfs-automation.known_host_refresh must be a list for {host}")

    entries: list[KnownHostRefresh] = []
    seen: set[tuple[str, str, int]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"zfs-automation.known_host_refresh[{index}] must be a mapping for {host}"
            )
        hostname = require_string(
            item.get("host", ""),
            f"zfs-automation.known_host_refresh[{index}].host required for {host}",
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", hostname):
            raise ValueError(
                f"zfs-automation.known_host_refresh[{index}].host is invalid for {host}"
            )
        known_hosts = str(item.get("known_hosts", "/root/.ssh/known_hosts")).strip()
        if not known_hosts.startswith("/root/.ssh/") or known_hosts.endswith("/"):
            raise ValueError(
                "zfs-automation.known_host_refresh known_hosts must be a root .ssh file "
                f"for {host}"
            )
        port_raw = item.get("port", 22)
        try:
            port = int(port_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"zfs-automation.known_host_refresh[{index}].port is invalid for {host}"
            ) from exc
        if port < 1 or port > 65535:
            raise ValueError(
                f"zfs-automation.known_host_refresh[{index}].port is invalid for {host}"
            )
        key = (hostname, known_hosts, port)
        if key in seen:
            raise ValueError(f"duplicate known_host_refresh entry {key} for {host}")
        seen.add(key)
        entries.append(KnownHostRefresh(host=hostname, known_hosts=known_hosts, port=port))
    return tuple(entries)


def rendered_private_key(root: Path, secret: str) -> str:
    text = op_secrets.secret_file(root, secret).read_text(encoding="utf-8")
    prefix = "ZFS_PUSH_PRIVATE_KEY="
    if text.startswith(prefix):
        text = text[len(prefix) :]
    text = text.strip()
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        text = text[1:-1]
    if not text.startswith("-----BEGIN ") or "PRIVATE KEY-----" not in text.splitlines()[0]:
        raise ValueError(f"secret '{secret}' did not render a private key")
    return text.rstrip("\n") + "\n"


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


def normalize_replication_job_name(value: object, host: str) -> str:
    name = require_string(value, f"replication job name required for {host}")
    if not REPLICATION_JOB_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"replication job name '{name}' for {host} must use only letters, numbers,"
            " underscores, or hyphens and must start with a letter or number"
        )
    return name


def normalize_migratable_lxc_group_name(value: object, host: str) -> str:
    name = require_string(value, f"migratable LXC group name required for {host}")
    if not REPLICATION_JOB_NAME_PATTERN.fullmatch(name):
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


def expand_migratable_lxc_replication_plans(
    registry,
    job_config: dict,
    explicit_plans: list,
    host: str,
    job_name: str,
) -> list[ReplicationPlan]:
    group = resolve_migratable_lxc_group(
        registry,
        job_config.get("migratable_lxc_group"),
        host,
        "migratable_lxc_group",
    )
    target_root = require_string(
        job_config.get("target_root", ""),
        f"target_root required for migratable LXC replication job '{job_name}' on {host}",
    ).rstrip("/")
    group_plans = {plan.name: plan for plan in group.plans}
    plans: list[ReplicationPlan] = []
    seen_targets: set[str] = set()
    for index, plan in enumerate(explicit_plans):
        if not isinstance(plan, dict):
            raise ValueError(f"invalid plan at index {index} in job '{job_name}' for {host}")
        name = require_safe_authorized_key_option(
            plan.get("name", ""),
            f"plan name required at index {index} in job '{job_name}' for {host}",
        )
        group_plan = group_plans.get(name)
        if group_plan is None:
            raise ValueError(
                f"plan '{name}' in job '{job_name}' does not exist in group {group.name}"
            )
        target = require_string(
            plan.get("target", ""),
            f"plan target required at index {index} in job '{job_name}' for {host}",
        )
        if ":" not in target and not target.startswith("/"):
            target = f"{target_root}/{target.lstrip('/')}"
        if target in seen_targets:
            raise ValueError(f"duplicate target {target} in job '{job_name}' for {host}")
        seen_targets.add(target)
        plans.append(
            ReplicationPlan(
                target=target,
                source=group_plan.dataset,
                require_active_lxc=group_plan.vmid,
            )
        )
    return plans


def require_safe_authorized_key_option(value: object, message: str) -> str:
    text = require_string(value, message)
    if any(char in text for char in ['"', "'", ",", " ", "\t", "\n", "\r"]):
        raise ValueError(message)
    return text


def dataset_pool(dataset: str) -> str:
    dataset_name = dataset.split(":", 1)[1] if ":" in dataset else dataset
    return dataset_name.split("/", 1)[0]


def is_remote_dataset(dataset: str) -> bool:
    return ":" in dataset


def normalize_dataset_under_root(dataset: str, root_dataset: str) -> str:
    if dataset == root_dataset or dataset.startswith(f"{root_dataset}/"):
        return dataset
    return f"{root_dataset}/{dataset}"


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
        require_active_lxc=(
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
        explicit_plans = group_config.get("plans", [])
        if not isinstance(explicit_plans, list) or not explicit_plans:
            raise ValueError(
                "migratable LXC group "
                f"'{normalized_group_name}' plans must be a non-empty list for {host}"
            )
        plans: list[MigratableLxcPlan] = []
        seen_names: set[str] = set()
        seen_datasets: set[str] = set()
        for index, plan in enumerate(explicit_plans):
            if not isinstance(plan, dict):
                raise ValueError(
                    "invalid migratable LXC plan at index "
                    f"{index} in group '{normalized_group_name}' for {host}"
                )
            name = require_safe_authorized_key_option(
                plan.get("name", f"plan-{index}"),
                "name must be safe for migratable LXC plan "
                f"{index} in group '{normalized_group_name}' for {host}",
            )
            if name in seen_names:
                raise ValueError(
                    f"duplicate plan name {name} in migratable LXC group "
                    f"'{normalized_group_name}' for {host}"
                )
            seen_names.add(name)
            vmid = normalize_positive_int(
                plan.get("vmid"),
                "vmid must be a positive integer for migratable LXC plan "
                f"{index} in group '{normalized_group_name}' for {host}",
            )
            dataset = require_string(
                plan.get("dataset", ""),
                "dataset required for migratable LXC plan "
                f"{index} in group '{normalized_group_name}' for {host}",
            )
            if dataset in seen_datasets:
                raise ValueError(
                    f"duplicate dataset {dataset} in migratable LXC group "
                    f"'{normalized_group_name}' for {host}"
                )
            seen_datasets.add(dataset)
            plans.append(MigratableLxcPlan(name=name, vmid=vmid, dataset=dataset))

        groups[normalized_group_name] = MigratableLxcGroup(
            name=normalized_group_name,
            plans=tuple(plans),
        )
    return groups


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
        snapshot_plan_from_config(
            {"require_active_lxc": plan.vmid},
            defaults,
            plan.dataset,
            host,
        )
        for plan in group.plans
    ]


def normalize_snapshot_plans(registry, host: str) -> list[SnapshotPlan]:
    snapshot_template = registry.get(host, "zfs-automation.snapshot_template", None)
    if snapshot_template is not None:
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
            raise ValueError(
                f"snapshot_template {source_host}:{template_name} not found for {host}"
            )
        defaults = template.get("snapshot_defaults", {})
        explicit = template.get("snapshot_plans", None)
    else:
        defaults = registry.get(host, "zfs-automation.snapshot_defaults", {})
        explicit = registry.get(host, "zfs-automation.snapshot_plans", None)

    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError(f"zfs-automation.snapshot_defaults must be a mapping for {host}")

    if explicit is not None:
        if not isinstance(explicit, list):
            raise ValueError(f"zfs-automation.snapshot_plans must be a list for {host}")
        plans: list[SnapshotPlan] = []
        seen: set[str] = set()
        for index, plan in enumerate(explicit):
            if not isinstance(plan, dict):
                raise ValueError(f"invalid snapshot plan at index {index} for {host}")
            expanded_plans = (
                expand_migratable_lxc_snapshot_group(
                    registry,
                    plan.get("migratable_lxc_group"),
                    defaults,
                    host,
                )
                if "migratable_lxc_group" in plan
                else [
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
            )
            for expanded_plan in expanded_plans:
                if expanded_plan.dataset in seen:
                    raise ValueError(
                        f"duplicate snapshot plan dataset {expanded_plan.dataset} for {host}"
                    )
                seen.add(expanded_plan.dataset)
                plans.append(expanded_plan)
        return plans

    if registry.get(host, "zfs-automation.sanoid", None) is not None:
        raise ValueError(
            f"zfs-automation.sanoid is no longer supported for {host}; use snapshot_plans"
        )

    return []
