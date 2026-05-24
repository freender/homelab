from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from shlex import quote

from ..build import copy_files, render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-zfs-automation"
REPLICATION_JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
STATIC_CONFIG_FILES = ["zfs-scrub.timer"]
TEMPLATE_FILES = [
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
    "homelab-zfs-replication.service",
    "homelab-zfs-replication.timer",
    "homelab-zfs-scrub.sh",
    "zfs-scrub.service",
    "homelab-zfs-health-check.service",
    "homelab-zfs-health-check.timer",
]


@dataclass(frozen=True)
class FileSpec:
    build_name: str
    remote_path: str
    mode: str = "644"


@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    file_specs: tuple[FileSpec, ...]


@dataclass(frozen=True)
class SnapshotPlan:
    dataset: str
    excludes: tuple[str, ...]
    hourly: str
    daily: str
    weekly: str
    monthly: str
    yearly: str
    recursive: bool = True
    process_children_only: bool = True
    auto_exclude_replication: bool = False
    require_active_lxc: int | None = None


@dataclass(frozen=True)
class DynamicLxcSourceCandidate:
    name: str
    source: str
    sshkey: str
    syncoid_options: tuple[str, ...]


@dataclass(frozen=True)
class DynamicLxcSource:
    vmid: int
    candidates: tuple[DynamicLxcSourceCandidate, ...]


@dataclass(frozen=True)
class MigratableLxcPlan:
    name: str
    vmid: int
    dataset: str


@dataclass(frozen=True)
class MigratableLxcGroup:
    name: str
    nodes: tuple[str, ...]
    plans: tuple[MigratableLxcPlan, ...]


@dataclass(frozen=True)
class ReplicationPlan:
    target: str
    source: str = ""
    dynamic_lxc_source: DynamicLxcSource | None = None
    post_hook: str = ""


@dataclass(frozen=True)
class TargetSnapshotPrune:
    keep_days: int
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class ReplicationJob:
    name: str
    schedule: str
    plans: tuple[ReplicationPlan, ...]
    after_commands: tuple[str, ...]
    syncoid_options: tuple[str, ...]
    delete_target_snapshots: bool
    target_snapshot_prune: TargetSnapshotPrune | None


@dataclass(frozen=True)
class ZfsPuller:
    name: str
    from_address: str
    public_key: str


@dataclass(frozen=True)
class ZfsPullSourceAccess:
    enabled: bool
    user: str
    datasets: tuple[str, ...]
    pullers: tuple[ZfsPuller, ...]


@dataclass(frozen=True)
class ZfsPusher:
    name: str
    from_address: str
    public_key: str


@dataclass(frozen=True)
class ZfsPushTargetAccess:
    enabled: bool
    user: str
    datasets: tuple[str, ...]
    pushers: tuple[ZfsPusher, ...]


BASE_FILE_SPECS = (
    FileSpec("sanoid.conf", "/etc/sanoid/sanoid.conf"),
    FileSpec(
        "homelab-zfs-snapshots.service",
        "/etc/systemd/system/homelab-zfs-snapshots.service",
    ),
    FileSpec("homelab-zfs-snapshots.timer", "/etc/systemd/system/homelab-zfs-snapshots.timer"),
    FileSpec("homelab-zfs-snapshots.sh", "/usr/local/bin/homelab-zfs-snapshots", mode="755"),
    FileSpec("homelab-zfs-scrub.sh", "/usr/local/bin/homelab-zfs-scrub", mode="755"),
    FileSpec("zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service"),
    FileSpec("zfs-scrub.timer", "/etc/systemd/system/zfs-scrub.timer"),
    FileSpec(
        "homelab-zfs-health-check.service",
        "/etc/systemd/system/homelab-zfs-health-check.service",
    ),
    FileSpec(
        "homelab-zfs-health-check.timer",
        "/etc/systemd/system/homelab-zfs-health-check.timer",
    ),
    FileSpec("homelab-zfs-health-check.sh", "/usr/local/bin/homelab-zfs-health-check", mode="755"),
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="zfs-automation")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping zfs-automation (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


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
        normalize_pull_source_access(registry, host)
        normalize_pull_source_access(registry, host, include_disabled=True)
        pools = resolve_pools(registry, host)
        if not pools:
            raise ValueError(f"zfs-automation requires at least one managed pool for {host}")


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


def normalize_snapshot_patterns(value: object, message: str) -> tuple[str, ...]:
    patterns = normalize_string_list(value, message)
    if not patterns:
        raise ValueError(message)
    for pattern in patterns:
        if any(char in pattern for char in ["/", "@", "\0", "\n", "\r"]):
            raise ValueError(message)
    return tuple(patterns)


def normalize_target_snapshot_prune(
    value: object,
    host: str,
    job_name: str,
) -> TargetSnapshotPrune | None:
    if value in (None, False):
        return None
    if not isinstance(value, dict):
        raise ValueError(f"target_snapshot_prune must be a mapping for job '{job_name}' on {host}")
    enabled = normalize_bool(
        value.get("enabled"),
        True,
        f"target_snapshot_prune.enabled must be true or false for job '{job_name}' on {host}",
    )
    if not enabled:
        return None
    return TargetSnapshotPrune(
        keep_days=normalize_positive_int(
            value.get("keep_days", 90),
            "target_snapshot_prune.keep_days must be a positive integer for job "
            f"'{job_name}' on {host}",
        ),
        patterns=normalize_snapshot_patterns(
            value.get("patterns", ["autosnap_*", "__replicate_*"]),
            "target_snapshot_prune.patterns must be a non-empty list of snapshot "
            f"name patterns for job '{job_name}' on {host}",
        ),
    )


def normalize_dynamic_lxc_source(
    value: object,
    host: str,
    job_name: str,
    plan_index: int,
) -> DynamicLxcSource:
    if not isinstance(value, dict):
        raise ValueError(
            "dynamic_lxc_source must be a mapping for plan "
            f"{plan_index} in job '{job_name}' on {host}"
        )

    vmid = normalize_positive_int(
        value.get("vmid"),
        f"dynamic_lxc_source.vmid must be a positive integer for job '{job_name}' on {host}",
    )
    candidates_config = value.get("candidates", [])
    if not isinstance(candidates_config, list) or not candidates_config:
        raise ValueError(
            "dynamic_lxc_source.candidates must be a non-empty list for job "
            f"'{job_name}' on {host}"
        )

    candidates: list[DynamicLxcSourceCandidate] = []
    seen_names: set[str] = set()
    for candidate_index, candidate_config in enumerate(candidates_config):
        if not isinstance(candidate_config, dict):
            raise ValueError(
                "invalid dynamic_lxc_source candidate at index "
                f"{candidate_index} for job '{job_name}' on {host}"
            )
        name = require_safe_authorized_key_option(
            candidate_config.get("name", ""),
            "dynamic_lxc_source candidate name must be safe for job "
            f"'{job_name}' on {host}",
        )
        if name in seen_names:
            raise ValueError(
                f"duplicate dynamic_lxc_source candidate '{name}' for job '{job_name}' on {host}"
            )
        seen_names.add(name)

        candidates.append(
            DynamicLxcSourceCandidate(
                name=name,
                source=require_string(
                    candidate_config.get("source", ""),
                    "dynamic_lxc_source candidate source required for job "
                    f"'{job_name}' on {host}",
                ),
                sshkey=require_string(
                    candidate_config.get("sshkey", ""),
                    "dynamic_lxc_source candidate sshkey required for job "
                    f"'{job_name}' on {host}",
                ),
                syncoid_options=tuple(
                    normalize_string_list(
                        candidate_config.get("syncoid_options", []),
                        "dynamic_lxc_source candidate syncoid_options must be a list for job "
                        f"'{job_name}' on {host}",
                    )
                ),
            )
        )

    return DynamicLxcSource(vmid=vmid, candidates=tuple(candidates))


def node_mgmt_ip(registry, node: str) -> str:
    mgmt_ip = require_string(
        registry.get(node, "pve-postinstall.interfaces.mgmt_ip", ""),
        f"pve-postinstall.interfaces.mgmt_ip required for dynamic LXC source node {node}",
    )
    return mgmt_ip.split("/", 1)[0]


def normalize_dynamic_lxc_source_from_candidates(
    registry,
    plan: dict,
    candidate_names: list[str],
    host: str,
    job_name: str,
    plan_index: int,
) -> DynamicLxcSource:
    vmid = normalize_positive_int(
        plan.get("vmid"),
        "vmid must be a positive integer for dynamic plan "
        f"{plan_index} in job '{job_name}' on {host}",
    )
    dataset = require_string(
        plan.get("dataset", ""),
        f"dataset required for dynamic plan {plan_index} in job '{job_name}' on {host}",
    )

    candidates = []
    seen_names: set[str] = set()
    for candidate_name in candidate_names:
        name = require_safe_authorized_key_option(
            candidate_name,
            f"dynamic_lxc_candidates contains invalid node name for job '{job_name}' on {host}",
        )
        if name in seen_names:
            raise ValueError(
                f"duplicate dynamic_lxc_candidates entry '{name}' for job '{job_name}' on {host}"
            )
        seen_names.add(name)
        source = f"zfs-pull@{node_mgmt_ip(registry, name)}:{dataset}"
        candidates.append(
            DynamicLxcSourceCandidate(
                name=name,
                source=source,
                sshkey=f"/root/.ssh/homelab-zfs-pull_{name}_ed25519",
                syncoid_options=(f"--identifier={name}",),
            )
        )

    return DynamicLxcSource(vmid=vmid, candidates=tuple(candidates))


def normalize_string_map(value: object, message: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(message)
    return {
        require_safe_authorized_key_option(key, message): require_string(item, message)
        for key, item in value.items()
    }


def normalize_dynamic_lxc_source_from_group(
    registry,
    group: MigratableLxcGroup,
    group_plan: MigratableLxcPlan,
    candidate_names: list[str],
    candidate_addresses: dict[str, str],
    candidate_sshkeys: dict[str, str],
    host: str,
    job_name: str,
) -> DynamicLxcSource:
    candidates = []
    seen_names: set[str] = set()
    for candidate_name in candidate_names:
        name = require_safe_authorized_key_option(
            candidate_name,
            f"dynamic LXC candidate node is invalid for job '{job_name}' on {host}",
        )
        if name in seen_names:
            raise ValueError(
                f"duplicate dynamic LXC candidate '{name}' for job '{job_name}' on {host}"
            )
        seen_names.add(name)
        address = candidate_addresses.get(name, node_mgmt_ip(registry, name))
        sshkey = candidate_sshkeys.get(name, f"/root/.ssh/homelab-zfs-pull_{name}_ed25519")
        source = f"zfs-pull@{address}:{group_plan.dataset}"
        candidates.append(
            DynamicLxcSourceCandidate(
                name=name,
                source=source,
                sshkey=sshkey,
                syncoid_options=(f"--identifier={name}",),
            )
        )

    if not candidates:
        raise ValueError(f"migratable LXC group '{group.name}' has no candidates for {host}")
    return DynamicLxcSource(vmid=group_plan.vmid, candidates=tuple(candidates))


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
    candidate_names = normalize_string_list(
        job_config.get("dynamic_lxc_candidates", list(group.nodes)),
        f"dynamic_lxc_candidates for job '{job_name}' must be a list for {host}",
    )
    candidate_addresses = normalize_string_map(
        job_config.get("dynamic_lxc_candidate_addresses", {}),
        f"dynamic_lxc_candidate_addresses for job '{job_name}' must be a mapping for {host}",
    )
    candidate_sshkeys = normalize_string_map(
        job_config.get("dynamic_lxc_candidate_sshkeys", {}),
        f"dynamic_lxc_candidate_sshkeys for job '{job_name}' must be a mapping for {host}",
    )
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
                dynamic_lxc_source=normalize_dynamic_lxc_source_from_group(
                    registry,
                    group,
                    group_plan,
                    candidate_names,
                    candidate_addresses,
                    candidate_sshkeys,
                    host,
                    job_name,
                ),
                post_hook=str(plan.get("post_hook", "")).strip(),
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
    if "exclude" in plan and "excludes" in plan:
        raise ValueError(
            f"snapshot plan for {dataset} on {host} specifies both 'exclude' and 'excludes'; "
            "use only 'exclude'"
        )
    excludes = normalize_string_list(
        plan.get("exclude", plan.get("excludes", [])),
        f"snapshot plan excludes must be a list for {host}",
    )
    return SnapshotPlan(
        dataset=dataset,
        excludes=tuple(excludes),
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
        auto_exclude_replication=True,
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
        nodes = normalize_string_list(
            group_config.get("nodes", []),
            f"migratable LXC group '{normalized_group_name}' nodes must be a list for {host}",
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
            nodes=tuple(nodes),
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
    defaults = registry.get(host, "zfs-automation.snapshot_defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError(f"zfs-automation.snapshot_defaults must be a mapping for {host}")

    explicit = registry.get(host, "zfs-automation.snapshot_plans", None)
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


def normalize_replication_config(
    registry, host: str, *, include_disabled: bool = False
) -> list[ReplicationJob]:
    defaults = registry.get(host, "zfs-automation.replication_defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError(f"zfs-automation.replication_defaults must be a mapping for {host}")

    for legacy_key in ("replication_plans", "replication"):
        if registry.get(host, f"zfs-automation.{legacy_key}", None) is not None:
            raise ValueError(
                f"zfs-automation.{legacy_key} is no longer supported for {host}; "
                "use replication_jobs"
            )

    jobs = registry.get(host, "zfs-automation.replication_jobs", None)
    if jobs is None:
        return []
    if not isinstance(jobs, dict):
        raise ValueError(f"zfs-automation.replication_jobs must be a dict for {host}")

    default_after_commands = normalize_string_list(
        defaults.get("after_replication_commands", []),
        f"replication_defaults.after_replication_commands must be a list for {host}",
    )
    default_syncoid_options = normalize_string_list(
        defaults.get("syncoid_options", []),
        f"replication_defaults.syncoid_options must be a list for {host}",
    )
    default_delete_target_snapshots = normalize_bool(
        defaults.get("delete_target_snapshots"),
        True,
        f"replication_defaults.delete_target_snapshots must be true or false for {host}",
    )
    default_target_snapshot_prune = normalize_target_snapshot_prune(
        defaults.get("target_snapshot_prune"),
        host,
        "replication_defaults",
    )

    parsed_jobs: list[ReplicationJob] = []
    seen_job_names: set[str] = set()
    for job_name, job_config in jobs.items():
        if not isinstance(job_config, dict):
            raise ValueError(f"invalid replication job '{job_name}' for {host}")

        normalized_job_name = normalize_replication_job_name(job_name, host)
        if normalized_job_name in seen_job_names:
            raise ValueError(f"duplicate replication job name '{normalized_job_name}' for {host}")
        seen_job_names.add(normalized_job_name)

        enabled = normalize_bool(
            job_config.get("enabled"),
            True,
            f"enabled for replication job '{normalized_job_name}' must be true or false for {host}",
        )
        if not enabled and not include_disabled:
            continue

        schedule = str(job_config.get("schedule", defaults.get("schedule", "*-*-* 02:30:00")))
        explicit_plans = job_config.get("plans", [])
        if not isinstance(explicit_plans, list):
            raise ValueError(
                f"plans for replication job '{normalized_job_name}' must be a list for {host}"
            )
        if "migratable_lxc_group" in job_config:
            plans = expand_migratable_lxc_replication_plans(
                registry,
                job_config,
                explicit_plans,
                host,
                normalized_job_name,
            )
        else:
            plans = []
            dynamic_lxc_candidates = normalize_string_list(
                job_config.get("dynamic_lxc_candidates", []),
                f"dynamic_lxc_candidates for job '{normalized_job_name}' must be a list for {host}",
            )

            for index, plan in enumerate(explicit_plans):
                if not isinstance(plan, dict):
                    raise ValueError(
                        f"invalid plan at index {index} in job '{normalized_job_name}' for {host}"
                    )
                dynamic_lxc_source = (
                    normalize_dynamic_lxc_source(
                        plan.get("dynamic_lxc_source"),
                        host,
                        normalized_job_name,
                        index,
                    )
                    if "dynamic_lxc_source" in plan
                    else normalize_dynamic_lxc_source_from_candidates(
                        registry,
                        plan,
                        dynamic_lxc_candidates,
                        host,
                        normalized_job_name,
                        index,
                    )
                    if dynamic_lxc_candidates and "vmid" in plan and "dataset" in plan
                    else None
                )
                source = str(plan.get("source", "")).strip()
                if bool(source) == bool(dynamic_lxc_source):
                    raise ValueError(
                        f"plan at index {index} in job '{normalized_job_name}' for {host} "
                        "must specify "
                        "exactly one of source or dynamic_lxc_source"
                    )
                plans.append(
                    ReplicationPlan(
                        target=require_string(
                            plan.get("target", ""),
                            f"plan target required at index {index} in job"
                            f" '{normalized_job_name}' for {host}",
                        ),
                        source=source,
                        dynamic_lxc_source=dynamic_lxc_source,
                        post_hook=str(plan.get("post_hook", "")).strip(),
                    )
                )

        after_commands = [
            *default_after_commands,
            *normalize_string_list(
                job_config.get("after_replication_commands", []),
                f"after_replication_commands for job '{normalized_job_name}' must be a list"
                f" for {host}",
            ),
        ]
        syncoid_options = [
            *default_syncoid_options,
            *normalize_string_list(
                job_config.get("syncoid_options", []),
                f"syncoid_options for job '{normalized_job_name}' must be a list for {host}",
            ),
        ]
        delete_target_snapshots = normalize_bool(
            job_config.get("delete_target_snapshots"),
            default_delete_target_snapshots,
            "delete_target_snapshots for replication job "
            f"'{normalized_job_name}' must be true or false for {host}",
        )
        target_snapshot_prune = (
            normalize_target_snapshot_prune(
                job_config.get("target_snapshot_prune"),
                host,
                normalized_job_name,
            )
            if "target_snapshot_prune" in job_config
            else default_target_snapshot_prune
        )

        parsed_jobs.append(
            ReplicationJob(
                name=normalized_job_name,
                schedule=schedule,
                plans=tuple(plans),
                after_commands=tuple(after_commands),
                syncoid_options=tuple(syncoid_options),
                delete_target_snapshots=delete_target_snapshots,
                target_snapshot_prune=target_snapshot_prune,
            )
        )
    return parsed_jobs


def normalize_pull_source_access(
    registry,
    host: str,
    *,
    include_disabled: bool = False,
) -> ZfsPullSourceAccess | None:
    config = registry.get(host, "zfs-automation.pull_source_access", None)
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError(f"zfs-automation.pull_source_access must be a mapping for {host}")

    enabled = normalize_bool(
        config.get("enabled"),
        True,
        f"zfs-automation.pull_source_access.enabled must be true or false for {host}",
    )
    if not enabled and not include_disabled:
        return None

    user = require_safe_authorized_key_option(
        config.get("user", "zfs-pull"),
        f"zfs-automation.pull_source_access.user is invalid for {host}",
    )
    datasets = normalize_string_list(
        config.get("datasets", []),
        f"zfs-automation.pull_source_access.datasets must be a list for {host}",
    )
    if not datasets:
        raise ValueError(f"zfs-automation.pull_source_access.datasets is required for {host}")

    puller_configs = config.get("allowed_pullers", [])
    if not isinstance(puller_configs, list) or not puller_configs:
        raise ValueError(
            f"zfs-automation.pull_source_access.allowed_pullers must be a non-empty list"
            f" for {host}"
        )
    pullers: list[ZfsPuller] = []
    for index, puller_config in enumerate(puller_configs):
        if not isinstance(puller_config, dict):
            raise ValueError(f"invalid pull source allowed_puller at index {index} for {host}")
        name = require_safe_authorized_key_option(
            puller_config.get("name", ""),
            f"puller name required at index {index} for {host}",
        )
        from_address = require_safe_authorized_key_option(
            puller_config.get("from", ""),
            f"puller from address required at index {index} for {host}",
        )
        public_key = require_string(
            puller_config.get("public_key", ""),
            f"puller public_key required at index {index} for {host}",
        )
        if not public_key.startswith(("ssh-ed25519 ", "sk-ssh-ed25519@openssh.com ")):
            raise ValueError(f"puller public_key at index {index} for {host} must be ed25519")
        pullers.append(ZfsPuller(name=name, from_address=from_address, public_key=public_key))

    return ZfsPullSourceAccess(
        enabled=enabled,
        user=user,
        datasets=tuple(datasets),
        pullers=tuple(pullers),
    )


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
            *(
                [candidate.source for candidate in plan.dynamic_lxc_source.candidates]
                if plan.dynamic_lxc_source
                else []
            ),
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


def sanoid_plan_lines(plan: SnapshotPlan, replication_datasets: set[str]) -> list[str]:
    lines = [
        f"[{plan.dataset}]",
        f"recursive = {'yes' if plan.recursive else 'no'}",
        f"process_children_only = {'yes' if plan.process_children_only else 'no'}",
        f"hourly = {plan.hourly}",
        f"daily = {plan.daily}",
        f"weekly = {plan.weekly}",
        f"monthly = {plan.monthly}",
        f"yearly = {plan.yearly}",
        "autosnap = yes",
        "autoprune = yes",
        "",
    ]

    excluded = {normalize_dataset_under_root(dataset, plan.dataset) for dataset in plan.excludes}
    if plan.auto_exclude_replication:
        excluded.update(
            replication_exclude
            for dataset in replication_datasets
            for replication_exclude in local_replication_excludes(dataset, plan.dataset)
        )

    for dataset in sorted(excluded):
        lines.extend([f"[{dataset}]", "autosnap = no", "autoprune = no", ""])

    return lines


def build_sanoid_config(
    snapshot_plans: list[SnapshotPlan],
    replication_jobs: list[ReplicationJob],
) -> str:
    lines: list[str] = []
    replication_datasets = {plan.target for job in replication_jobs for plan in job.plans}
    for plan in snapshot_plans:
        lines.extend(sanoid_plan_lines(plan, replication_datasets))

    return "\n".join(lines).rstrip() + "\n"


def append_snapshot_plan_script(
    lines: list[str],
    plan: SnapshotPlan,
    config_lines: list[str],
) -> None:
    lines.extend(
        [
            f"echo {quote(f'Including snapshot plan: {plan.dataset}')}",
            "cat >> \"$CONFIG_FILE\" <<'SANOID_CONFIG'",
            *config_lines,
            "SANOID_CONFIG",
            "PLAN_COUNT=$((PLAN_COUNT + 1))",
            "",
        ]
    )


def build_snapshot_script(
    snapshot_plans: list[SnapshotPlan],
    replication_jobs: list[ReplicationJob],
) -> str:
    replication_datasets = {plan.target for job in replication_jobs for plan in job.plans}
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        'CONFIG_DIR="$(mktemp -d)"',
        "# shellcheck disable=SC2034",
        'CONFIG_FILE="$CONFIG_DIR/sanoid.conf"',
        'trap \'rm -rf "$CONFIG_DIR"\' EXIT',
        "PLAN_COUNT=0",
        "",
        "require_dataset() {",
        "  local dataset=\"$1\"",
        "  if ! zfs list -H -o name \"$dataset\" >/dev/null 2>&1; then",
        "    echo \"ERROR: active snapshot dataset missing: $dataset\" >&2",
        "    exit 1",
        "  fi",
        "}",
        "",
    ]

    if not snapshot_plans:
        lines.extend(["echo 'No snapshot plans configured; nothing to do'", "exit 0", ""])
    else:
        for plan in snapshot_plans:
            config_lines = sanoid_plan_lines(plan, replication_datasets)
            if plan.require_active_lxc is None:
                append_snapshot_plan_script(lines, plan, config_lines)
                continue

            unit_name = f"pve-container@{plan.require_active_lxc}.service"
            active_message = f"Active LXC {plan.require_active_lxc}; snapshotting {plan.dataset}"
            skip_message = (
                f"Skipping {plan.dataset}; LXC {plan.require_active_lxc} is not active locally"
            )
            lines.extend(
                [
                    f"if systemctl is-active --quiet {quote(unit_name)}; then",
                    f"  echo {quote(active_message)}",
                    f"  require_dataset {quote(plan.dataset)}",
                    "  cat >> \"$CONFIG_FILE\" <<'SANOID_CONFIG'",
                    *config_lines,
                    "SANOID_CONFIG",
                    "  PLAN_COUNT=$((PLAN_COUNT + 1))",
                    "else",
                    f"  echo {quote(skip_message)}",
                    "fi",
                    "",
                ]
            )

    lines.extend(
        [
            "if [[ $PLAN_COUNT -eq 0 ]]; then",
            "  echo 'No active snapshot plans selected; nothing to do'",
            "  exit 0",
            "fi",
            "",
            "/usr/sbin/sanoid --configdir=\"$CONFIG_DIR\" --cron --verbose",
            "",
        ]
    )
    return "\n".join(lines)


def local_replication_excludes(dataset: str, root_dataset: str) -> list[str]:
    if is_remote_dataset(dataset):
        return []
    if dataset != root_dataset and not dataset.startswith(f"{root_dataset}/"):
        return []

    parts = dataset.split("/")
    root_parts = root_dataset.split("/")
    excludes = []
    for index in range(len(root_parts) + 1, len(parts) + 1):
        excludes.append("/".join(parts[:index]))
    return excludes


def shell_array_block(name: str, values: list[str]) -> str:
    lines = [f"{name}=("]
    for value in values:
        lines.append(f"  {quote(value)}")
    lines.append(")")
    return "\n".join(lines)


def build_replication_script(
    replication_plans: list[ReplicationPlan],
    after_commands: list[str],
    syncoid_options: list[str],
    delete_target_snapshots: bool,
    target_snapshot_prune: TargetSnapshotPrune | None,
) -> str:
    def candidate_options(candidate: DynamicLxcSourceCandidate) -> list[str]:
        options = list(candidate.syncoid_options)
        if not any(option.startswith("--sshkey=") for option in options):
            options.append(f"--sshkey={candidate.sshkey}")
        return options

    syncoid_options_block = shell_array_block("SYNCOID_OPTIONS", syncoid_options)
    prune_config_lines: list[str] = []
    if target_snapshot_prune:
        prune_config_lines = [
            shell_array_block(
                "TARGET_PRUNE_PATTERNS",
                list(target_snapshot_prune.patterns),
            ),
            f"TARGET_PRUNE_KEEP_DAYS={target_snapshot_prune.keep_days}",
            "",
        ]
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        syncoid_options_block,
        "",
        *prune_config_lines,
        'SCRIPT_LOCK_FILE="/run/lock/$(basename "$0").lock"',
        'GLOBAL_LOCK_FILE="/run/lock/homelab-zfs-replication.lock"',
        'exec 9>"$SCRIPT_LOCK_FILE"',
        "if ! flock -n 9; then",
        '  echo "$(basename "$0") is already running; exiting"',
        "  exit 0",
        "fi",
        'exec 8>"$GLOBAL_LOCK_FILE"',
        "flock 8",
        "",
        "wait_for_existing_replication() {",
        "  while pgrep -af '(/usr/sbin/syncoid|zfs receive|zfs send)' >/dev/null; do",
        '    echo "Waiting for existing ZFS replication process to finish"',
        "    pgrep -af '(/usr/sbin/syncoid|zfs receive|zfs send)' || true",
        "    sleep 300",
        "  done",
        "}",
        "",
        "sshkey_from_options() {",
        "  local option",
        "  for option in \"$@\"; do",
        "    case \"$option\" in",
        "      --sshkey=*) printf '%s\\n' \"${option#--sshkey=}\"; return 0 ;;",
        "    esac",
        "  done",
        "  return 1",
        "}",
        "",
        "list_snapshot_names() {",
        "  local dataset_ref=\"$1\"",
        "  shift",
        "  local dataset remote sshkey",
        "",
        "  if [[ \"$dataset_ref\" == *:* ]]; then",
        "    remote=\"${dataset_ref%%:*}\"",
        "    dataset=\"${dataset_ref#*:}\"",
        "    if sshkey=\"$(sshkey_from_options \"$@\")\" || \\",
        "      sshkey=\"$(sshkey_from_options \"${SYNCOID_OPTIONS[@]}\")\"; then",
        "      ssh -i \"$sshkey\" \"$remote\" zfs list -H -t snapshot -o name \\",
        "        -s creation \"$dataset\" | sed \"s#^${dataset}@##\"",
        "    else",
        "      ssh \"$remote\" zfs list -H -t snapshot -o name -s creation \\",
        "        \"$dataset\" | sed \"s#^${dataset}@##\"",
        "    fi",
        "  else",
        "    dataset=\"$dataset_ref\"",
        "    zfs list -H -t snapshot -o name -s creation \"$dataset\" | sed \"s#^${dataset}@##\"",
        "  fi",
        "}",
        "",
        "require_common_snapshot_lineage() {",
        "  local source=\"$1\"",
        "  local target=\"$2\"",
        "  shift 2",
        "  local source_snaps target_snaps common_snaps",
        "",
        "  if ! zfs list -H -o name \"$target\" >/dev/null 2>&1; then",
        "    return 0",
        "  fi",
        "",
        "  source_snaps=\"$(mktemp)\"",
        "  target_snaps=\"$(mktemp)\"",
        "  common_snaps=\"$(mktemp)\"",
        "",
        "  list_snapshot_names \"$source\" \"$@\" | LC_ALL=C sort -u > \"$source_snaps\"",
        "  list_snapshot_names \"$target\" | LC_ALL=C sort -u > \"$target_snaps\"",
        "",
        "  if [[ ! -s \"$target_snaps\" ]]; then",
        "    echo \"ERROR: target $target exists but has no snapshots; refusing\" \\",
        "      \"replication without known lineage\" >&2",
        "    exit 1",
        "  fi",
        "  if [[ ! -s \"$source_snaps\" ]]; then",
        "    echo \"ERROR: source $source has no snapshots; refusing replication\" >&2",
        "    exit 1",
        "  fi",
        "",
        "  LC_ALL=C comm -12 \"$source_snaps\" \"$target_snaps\" > \"$common_snaps\"",
        "  if [[ ! -s \"$common_snaps\" ]]; then",
        "    echo \"ERROR: source $source and target $target have no common\" \\",
        "      \"snapshots; refusing destructive replication\" >&2",
        "    exit 1",
        "  fi",
        "",
        "  rm -f \"$source_snaps\" \"$target_snaps\" \"$common_snaps\"",
        "}",
        "",
        "remote_lxc_active() {",
        "  local remote=\"$1\"",
        "  local sshkey=\"$2\"",
        "  local vmid=\"$3\"",
        "  ssh -i \"$sshkey\" -o BatchMode=yes \"$remote\" homelab-lxc-active \"$vmid\"",
        "}",
        "",
        "wait_for_existing_replication",
        "",
    ]
    if target_snapshot_prune:
        lines.extend(
            [
                "snapshot_matches_target_prune_pattern() {",
                "  local snapshot_name=\"$1\"",
                "  local pattern",
                "  for pattern in \"${TARGET_PRUNE_PATTERNS[@]}\"; do",
                "    # shellcheck disable=SC2254",
                "    case \"$snapshot_name\" in",
                "      $pattern) return 0 ;;",
                "    esac",
                "  done",
                "  return 1",
                "}",
                "",
                "prune_target_snapshots() {",
                "  local dataset=\"$1\"",
                "  local cutoff snapshot created snapshot_name",
                "",
                "  if ! zfs list -H -o name \"$dataset\" >/dev/null 2>&1; then",
                "    echo \"Target prune skipped: dataset $dataset does not exist\"",
                "    return 0",
                "  fi",
                "",
                "  cutoff=$(($(date +%s) - TARGET_PRUNE_KEEP_DAYS * 86400))",
                "  while IFS=$'\\t' read -r snapshot created; do",
                "    [[ -n \"$snapshot\" && -n \"$created\" ]] || continue",
                "    snapshot_name=\"${snapshot#*@}\"",
                "    snapshot_matches_target_prune_pattern \"$snapshot_name\" || continue",
                "    [[ \"$created\" =~ ^[0-9]+$ ]] || continue",
                "    if (( created < cutoff )); then",
                "      echo \"Destroying old target snapshot $snapshot\"",
                "      zfs destroy \"$snapshot\"",
                "    fi",
                "  done < <(zfs list -H -p -r -t snapshot -o name,creation "
                "-s creation \"$dataset\")",
                "}",
                "",
            ]
        )
    if not replication_plans:
        lines.extend(["echo 'No replication plans configured; nothing to do'", ""])
    else:
        for plan in replication_plans:
            if plan.dynamic_lxc_source:
                resolve_message = (
                    f"Resolving active LXC {plan.dynamic_lxc_source.vmid} source for {plan.target}"
                )
                lines.extend(
                    [
                        f"echo {quote(resolve_message)}",
                        "SOURCE=''",
                        "ACTIVE_COUNT=0",
                        "SOURCE_OPTIONS=()",
                    ]
                )
                for candidate in plan.dynamic_lxc_source.candidates:
                    remote = candidate.source.split(":", 1)[0]
                    lines.extend(
                        [
                            f"if remote_lxc_active {quote(remote)} {quote(candidate.sshkey)} "
                            f"{quote(str(plan.dynamic_lxc_source.vmid))}; then",
                            f"  echo {quote(f'Active source candidate: {candidate.name}')}",
                            "  ACTIVE_COUNT=$((ACTIVE_COUNT + 1))",
                            f"  SOURCE={quote(candidate.source)}",
                            *(
                                f"  {line}"
                                for line in shell_array_block(
                                    "SOURCE_OPTIONS",
                                    candidate_options(candidate),
                                ).splitlines()
                            ),
                            "fi",
                        ]
                    )
                lines.extend(
                    [
                        "if [[ $ACTIVE_COUNT -ne 1 ]]; then",
                        "  echo \"ERROR: expected exactly one active LXC source; "
                        "found $ACTIVE_COUNT\" >&2",
                        "  exit 1",
                        "fi",
                        f"require_common_snapshot_lineage \"$SOURCE\" {quote(plan.target)} "
                        '"${SOURCE_OPTIONS[@]}"',
                    ]
                )
                command = [
                    "/usr/sbin/syncoid",
                    "-r",
                    '"${SYNCOID_OPTIONS[@]}"',
                    '"${SOURCE_OPTIONS[@]}"',
                    '"$SOURCE"',
                    plan.target,
                ]
            else:
                lines.append(
                    f"require_common_snapshot_lineage {quote(plan.source)} {quote(plan.target)}"
                )
                command = [
                    "/usr/sbin/syncoid",
                    "-r",
                    '"${SYNCOID_OPTIONS[@]}"',
                    plan.source,
                    plan.target,
                ]

            if delete_target_snapshots:
                command[2:2] = ["--delete-target-snapshots", "--force-delete"]
            lines.append(
                " ".join(
                    item
                    if item in {
                        '"${SYNCOID_OPTIONS[@]}"',
                        '"${SOURCE_OPTIONS[@]}"',
                        '"$SOURCE"',
                    }
                    else quote(item)
                    for item in command
                )
            )
            if plan.post_hook:
                lines.append(plan.post_hook)
            if target_snapshot_prune and not is_remote_dataset(plan.target):
                lines.append(f"prune_target_snapshots {quote(plan.target)}")
            lines.append("")

    for command in after_commands:
        lines.append(command)
    lines.append("")
    return "\n".join(lines)


def build_zfs_pull_source_authorized_keys(access: ZfsPullSourceAccess) -> str:
    allowed_roots = " ".join(access.datasets)
    lines = []
    for puller in access.pullers:
        options = [
            f'from="{puller.from_address}"',
            "restrict",
            f'command="/usr/local/sbin/homelab-zfs-send-only {allowed_roots}"',
        ]
        lines.append(f"{','.join(options)} {puller.public_key}")
    return "\n".join(lines) + "\n"


def build_zfs_push_target_authorized_keys(access: ZfsPushTargetAccess) -> str:
    allowed_roots = " ".join(access.datasets)
    lines = []
    for pusher in access.pushers:
        options = [
            f'from="{pusher.from_address}"',
            "restrict",
            f'command="/usr/local/sbin/homelab-zfs-receive-only {allowed_roots}"',
        ]
        lines.append(f"{','.join(options)} {pusher.public_key}")
    return "\n".join(lines) + "\n"


def build_zfs_pull_source_wrapper() -> str:
    return r"""#!/bin/bash

set -euo pipefail

deny() {
    printf 'Denied ZFS pull command: %s\n' "${SSH_ORIGINAL_COMMAND:-}" >&2
    exit 1
}

ALLOWED_ROOTS=("$@")
COMMAND="${SSH_ORIGINAL_COMMAND:-}"

[[ ${#ALLOWED_ROOTS[@]} -gt 0 ]] || deny
[[ -n "$COMMAND" ]] || deny

case "$COMMAND" in
    "exit") exit 0 ;;
    "echo -n") printf '' ; exit 0 ;;
esac

case "$COMMAND" in
    *';'*|*'&'*|*'`'*|*'$'*|*'<'*|*'>'*|*$'\n'*|*$'\r'*) deny ;;
esac

case "$COMMAND" in
    homelab-lxc-active\ *)
        vmid="${COMMAND#homelab-lxc-active }"
        [[ "$vmid" =~ ^[1-9][0-9]{1,8}$ ]] || deny
        exec /usr/bin/systemctl is-active --quiet "pve-container@${vmid}.service"
        ;;
esac

trim() {
    local text="$1"
    text="${text#"${text%%[![:space:]]*}"}"
    text="${text%"${text##*[![:space:]]}"}"
    printf '%s' "$text"
}

split_args() {
    local segment="$1"
    local -n out_ref="$2"
    read -r -a out_ref <<< "$segment"
    for index in "${!out_ref[@]}"; do
        out_ref[$index]="${out_ref[$index]//\'/}"
        out_ref[$index]="${out_ref[$index]//\"/}"
    done
}

case "$COMMAND" in
    "command -v mbuffer"|"command -v lzop"|"command -v pv")
        command -v "${COMMAND##* }" || exit 1
        exit 0
        ;;
esac

split_args "$COMMAND" ARGV

if [[ "${ARGV[0]:-}" == "zpool" \
    || "${ARGV[0]:-}" == "/sbin/zpool" \
    || "${ARGV[0]:-}" == "/usr/sbin/zpool" ]]; then
    [[ "${ARGV[1]:-}" == "get" ]] || deny
    [[ "${ARGV[2]:-}" == "-o" && "${ARGV[3]:-}" == "value" ]] || deny
    [[ "${ARGV[4]:-}" == "-H" && "${ARGV[5]:-}" == "feature@extensible_dataset" ]] || deny
    [[ ${#ARGV[@]} -eq 7 ]] || deny
    requested_pool="${ARGV[6]:-}"
    pool_allowed=false
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "${root%%/*}" == "$requested_pool" ]]; then
            pool_allowed=true
            break
        fi
    done
    [[ "$pool_allowed" == true ]] || deny
    exec /usr/sbin/zpool get -o value -H feature@extensible_dataset "$requested_pool"
fi

IFS='|' read -r -a PIPE_SEGMENTS <<< "$COMMAND"
[[ ${#PIPE_SEGMENTS[@]} -ge 1 && ${#PIPE_SEGMENTS[@]} -le 3 ]] || deny

ZFS_SEGMENT="$(trim "${PIPE_SEGMENTS[0]}")"
split_args "$ZFS_SEGMENT" ARGV

case "${ARGV[0]:-}" in
    zfs|/sbin/zfs|/usr/sbin/zfs) ;;
    *) deny ;;
esac

case "${ARGV[1]:-}" in
    list|get|send|hold|release) ;;
    *) deny ;;
esac

FOUND_DATASET=false
for token in "${ARGV[@]:2}"; do
    [[ "$token" == -* ]] && continue
    dataset="${token%%[@#]*}"

    allowed=false
    known_pool=false
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "$dataset" == "$root" || "$dataset" == "$root/"* ]]; then
            allowed=true
            break
        fi
        if [[ "$dataset" == "${root%%/*}" || "$dataset" == "${root%%/*}/"* ]]; then
            known_pool=true
        fi
    done
    if [[ "$allowed" == true ]]; then
        FOUND_DATASET=true
        continue
    fi
    [[ "$known_pool" == false ]] || deny
done

[[ "$FOUND_DATASET" == true ]] || deny

if [[ ${#PIPE_SEGMENTS[@]} -eq 1 ]]; then
    exec /usr/sbin/zfs "${ARGV[@]:1}"
fi

RUN_LZOP=false
RUN_MBUFFER=false
MBUFFER_ARGS=()

for segment in "${PIPE_SEGMENTS[@]:1}"; do
    segment="$(trim "$segment")"
    split_args "$segment" PIPE_ARGV
    case "${PIPE_ARGV[0]:-}" in
        lzop|/usr/bin/lzop)
            [[ ${#PIPE_ARGV[@]} -eq 1 ]] || deny
            [[ "$RUN_LZOP" == false ]] || deny
            RUN_LZOP=true
            ;;
        mbuffer|/usr/bin/mbuffer)
            [[ "$RUN_MBUFFER" == false ]] || deny
            RUN_MBUFFER=true
            MBUFFER_ARGS=("${PIPE_ARGV[@]:1}")
            ;;
        *) deny ;;
    esac
done

if [[ "$RUN_MBUFFER" == true ]]; then
    index=0
    while [[ $index -lt ${#MBUFFER_ARGS[@]} ]]; do
        case "${MBUFFER_ARGS[$index]}" in
            -q)
                index=$((index + 1))
                ;;
            -s|-m)
                [[ $((index + 1)) -lt ${#MBUFFER_ARGS[@]} ]] || deny
                [[ "${MBUFFER_ARGS[$((index + 1))]}" =~ ^[0-9]+[kKmMgG]?$ ]] || deny
                index=$((index + 2))
                ;;
            *) deny ;;
        esac
    done
fi

if [[ "$RUN_LZOP" == true && "$RUN_MBUFFER" == true ]]; then
    /usr/sbin/zfs "${ARGV[@]:1}" | /usr/bin/lzop | /usr/bin/mbuffer "${MBUFFER_ARGS[@]}"
elif [[ "$RUN_LZOP" == true ]]; then
    /usr/sbin/zfs "${ARGV[@]:1}" | /usr/bin/lzop
elif [[ "$RUN_MBUFFER" == true ]]; then
    /usr/sbin/zfs "${ARGV[@]:1}" | /usr/bin/mbuffer "${MBUFFER_ARGS[@]}"
else
    deny
fi
"""


def build_zfs_push_target_wrapper() -> str:
    return r"""#!/bin/bash

set -euo pipefail

deny() {
    printf 'Denied ZFS push command: %s\n' "${SSH_ORIGINAL_COMMAND:-}" >&2
    exit 1
}

ALLOWED_ROOTS=("$@")
COMMAND="${SSH_ORIGINAL_COMMAND:-}"

[[ ${#ALLOWED_ROOTS[@]} -gt 0 ]] || deny
[[ -n "$COMMAND" ]] || deny

case "$COMMAND" in
    "exit") exit 0 ;;
    "echo -n") printf '' ; exit 0 ;;
    "ps -Ao args=") exec /bin/ps -Ao args= ;;
    "command -v mbuffer"|"command -v lzop"|"command -v pv")
        command -v "${COMMAND##* }" || exit 1
        exit 0
        ;;
esac

case "$COMMAND" in
    *'`'*|*'$'*|*'<'*|*$'\n'*|*$'\r'*) deny ;;
esac

trim() {
    local text="$1"
    text="${text#"${text%%[![:space:]]*}"}"
    text="${text%"${text##*[![:space:]]}"}"
    printf '%s' "$text"
}

split_args() {
    local segment="$1"
    local -n out_ref="$2"
    read -r -a out_ref <<< "$segment"
    for index in "${!out_ref[@]}"; do
        out_ref[$index]="${out_ref[$index]//\'/}"
        out_ref[$index]="${out_ref[$index]//\"/}"
    done
}

dataset_allowed() {
    local dataset="$1"
    local root
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "$dataset" == "$root" || "$dataset" == "$root/"* ]]; then
            return 0
        fi
    done
    return 1
}

split_args "$COMMAND" ARGV

if [[ "${ARGV[0]:-}" == "zpool" \
    || "${ARGV[0]:-}" == "/sbin/zpool" \
    || "${ARGV[0]:-}" == "/usr/sbin/zpool" ]]; then
    [[ "${ARGV[1]:-}" == "get" ]] || deny
    [[ "${ARGV[2]:-}" == "-o" && "${ARGV[3]:-}" == "value" ]] || deny
    [[ "${ARGV[4]:-}" == "-H" && "${ARGV[5]:-}" == "feature@extensible_dataset" ]] || deny
    [[ ${#ARGV[@]} -eq 7 ]] || deny
    requested_pool="${ARGV[6]:-}"
    pool_allowed=false
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "${root%%/*}" == "$requested_pool" ]]; then
            pool_allowed=true
            break
        fi
    done
    [[ "$pool_allowed" == true ]] || deny
    exec /usr/sbin/zpool get -o value -H feature@extensible_dataset "$requested_pool"
fi

if [[ "${ARGV[0]:-}" == "zfs" \
    || "${ARGV[0]:-}" == "/sbin/zfs" \
    || "${ARGV[0]:-}" == "/usr/sbin/zfs" ]]; then
    case "${ARGV[1]:-}" in
        list|get|hold|release)
            FOUND_DATASET=false
            for token in "${ARGV[@]:2}"; do
                [[ "$token" == -* ]] && continue
                dataset="${token%%[@#]*}"
                if dataset_allowed "$dataset"; then
                    FOUND_DATASET=true
                    break
                fi
            done
            [[ "$FOUND_DATASET" == true ]] || deny
            exec /usr/sbin/zfs "${ARGV[@]:1}"
            ;;
    esac
fi

IFS='|' read -r -a PIPE_SEGMENTS <<< "$COMMAND"
[[ ${#PIPE_SEGMENTS[@]} -ge 1 && ${#PIPE_SEGMENTS[@]} -le 3 ]] || deny

ZFS_SEGMENT="$(trim "${PIPE_SEGMENTS[-1]}")"
split_args "$ZFS_SEGMENT" ZFS_ARGV

case "${ZFS_ARGV[0]:-}" in
    zfs|/sbin/zfs|/usr/sbin/zfs) ;;
    *) deny ;;
esac

case "${ZFS_ARGV[1]:-}" in
    receive|recv) ;;
    *) deny ;;
esac

TARGET_DATASET="${ZFS_ARGV[-1]:-}"
[[ -n "$TARGET_DATASET" ]] || deny
dataset_allowed "$TARGET_DATASET" || deny

RUN_LZOP=false
RUN_MBUFFER=false
MBUFFER_ARGS=()
LEADING_COUNT=$((${#PIPE_SEGMENTS[@]} - 1))
for ((segment_index = 0; segment_index < LEADING_COUNT; segment_index++)); do
    segment="$(trim "${PIPE_SEGMENTS[$segment_index]}")"
    split_args "$segment" PIPE_ARGV
    case "${PIPE_ARGV[0]:-}" in
        lzop|/usr/bin/lzop)
            [[ "${PIPE_ARGV[1]:-}" == "-dfc" ]] || deny
            RUN_LZOP=true
            ;;
        mbuffer|/usr/bin/mbuffer)
            RUN_MBUFFER=true
            MBUFFER_ARGS=("${PIPE_ARGV[@]:1}")
            ;;
        *) deny ;;
    esac
done

if [[ "$RUN_MBUFFER" == true ]]; then
    index=0
    while [[ $index -lt ${#MBUFFER_ARGS[@]} ]]; do
        case "${MBUFFER_ARGS[$index]}" in
            -q) index=$((index + 1)) ;;
            -s|-m)
                [[ $((index + 1)) -lt ${#MBUFFER_ARGS[@]} ]] || deny
                [[ "${MBUFFER_ARGS[$((index + 1))]}" =~ ^[0-9]+[kKmMgG]?$ ]] || deny
                index=$((index + 2))
                ;;
            *) deny ;;
        esac
    done
fi

if [[ "$RUN_MBUFFER" == true && "$RUN_LZOP" == true ]]; then
    /usr/bin/mbuffer "${MBUFFER_ARGS[@]}" | /usr/bin/lzop -dfc | /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
elif [[ "$RUN_MBUFFER" == true ]]; then
    /usr/bin/mbuffer "${MBUFFER_ARGS[@]}" | /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
elif [[ "$RUN_LZOP" == true ]]; then
    /usr/bin/lzop -dfc | /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
else
    /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
fi
"""


def build_health_check_script(pools: list[str]) -> str:
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        shell_array_block("ZFS_POOLS", pools),
        "",
        "for pool in \"${ZFS_POOLS[@]}\"; do",
        "  /sbin/zpool status -x \"$pool\"",
        "done",
        "",
    ]
    return "\n".join(lines)


def resolve_remote_path(spec: FileSpec) -> str:
    return spec.remote_path


def write_file_map(build_dir: Path, artifacts: HostArtifacts) -> None:
    lines = [
        f"{spec.build_name}|{resolve_remote_path(spec)}|{spec.mode}"
        for spec in artifacts.file_specs
    ]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))

    module_dir = root / "zfs-automation"
    artifacts = build_host_artifacts(root, host)
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    print_sub("Comparing with remote configs...")
    diff_pairs = [
        (artifacts.build_dir / spec.build_name, resolve_remote_path(spec))
        for spec in artifacts.file_specs
    ]
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy zfs-automation to {host}")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    module_dir = root / "zfs-automation"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    homelab_state_dir = str(registry.get(host, "config.homelab_state_dir", "/var/lib/homelab"))
    snapshot_schedule = str(
        registry.get(host, "zfs-automation.snapshot_schedule", "*-*-* 00:00:00")
    )
    health_check_schedule = str(
        registry.get(host, "zfs-automation.health_check_schedule", "hourly")
    )
    manage_snapshots = normalize_bool(
        registry.get(host, "zfs-automation.manage_snapshots", None),
        True,
        f"zfs-automation.manage_snapshots must be true or false for {host}",
    )
    manage_replication = normalize_bool(
        registry.get(host, "zfs-automation.manage_replication", None),
        True,
        f"zfs-automation.manage_replication must be true or false for {host}",
    )
    manage_scrub = normalize_bool(
        registry.get(host, "zfs-automation.manage_scrub", None),
        True,
        f"zfs-automation.manage_scrub must be true or false for {host}",
    )
    manage_health_check = normalize_bool(
        registry.get(host, "zfs-automation.manage_health_check", None),
        True,
        f"zfs-automation.manage_health_check must be true or false for {host}",
    )
    pools = resolve_pools(registry, host)
    snapshot_plans = normalize_snapshot_plans(registry, host)
    replication_jobs = normalize_replication_config(
        registry,
        host,
    )
    replication_exclude_jobs = normalize_replication_config(
        registry,
        host,
        include_disabled=True,
    )
    pull_source_access = normalize_pull_source_access(registry, host)
    push_target_access = normalize_push_target_access(registry, host)

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
    (build_dir / "sanoid.conf").write_text(
        build_sanoid_config(snapshot_plans, replication_exclude_jobs),
        encoding="utf-8",
    )
    render_file(
        templates_dir / "homelab-zfs-snapshots.service",
        build_dir / "homelab-zfs-snapshots.service",
    )
    render_file(
        templates_dir / "homelab-zfs-snapshots.timer",
        build_dir / "homelab-zfs-snapshots.timer",
        SNAPSHOT_SCHEDULE=snapshot_schedule,
    )
    (build_dir / "homelab-zfs-snapshots.sh").write_text(
        build_snapshot_script(snapshot_plans, replication_exclude_jobs),
        encoding="utf-8",
    )

    file_specs = list(BASE_FILE_SPECS)
    for job in replication_jobs:
        script_name = f"homelab-zfs-replication-{job.name}.sh"
        service_name = f"homelab-zfs-replication-{job.name}.service"
        timer_name = f"homelab-zfs-replication-{job.name}.timer"

        render_file(
            templates_dir / "homelab-zfs-replication.service",
            build_dir / service_name,
            SCRIPT_PATH=f"/usr/local/bin/homelab-zfs-replication-{job.name}",
        )
        render_file(
            templates_dir / "homelab-zfs-replication.timer",
            build_dir / timer_name,
            REPLICATION_SCHEDULE=job.schedule,
        )
        (build_dir / script_name).write_text(
            build_replication_script(
                list(job.plans),
                list(job.after_commands),
                list(job.syncoid_options),
                job.delete_target_snapshots,
                job.target_snapshot_prune,
            ),
            encoding="utf-8",
        )
        file_specs.extend(
            [
                FileSpec(
                    service_name,
                    f"/etc/systemd/system/{service_name}",
                ),
                FileSpec(
                    timer_name,
                    f"/etc/systemd/system/{timer_name}",
                ),
                FileSpec(
                    script_name,
                    f"/usr/local/bin/homelab-zfs-replication-{job.name}",
                    mode="755",
                ),
            ]
        )

    if pull_source_access is not None:
        (build_dir / "homelab-zfs-send-only.sh").write_text(
            build_zfs_pull_source_wrapper(),
            encoding="utf-8",
        )
        (build_dir / "zfs-pull-datasets.conf").write_text(
            "\n".join(pull_source_access.datasets) + "\n",
            encoding="utf-8",
        )
        (build_dir / "zfs-pull-authorized-keys").write_text(
            build_zfs_pull_source_authorized_keys(pull_source_access),
            encoding="utf-8",
        )
        file_specs.extend(
            [
                FileSpec(
                    "homelab-zfs-send-only.sh",
                    "/usr/local/sbin/homelab-zfs-send-only",
                    mode="755",
                ),
                FileSpec("zfs-pull-datasets.conf", "/etc/homelab/zfs-pull-datasets.conf"),
                FileSpec(
                    "zfs-pull-authorized-keys",
                    "/var/lib/homelab-zfs-pull/.ssh/authorized_keys",
                    mode="600",
                ),
            ]
        )

    if push_target_access is not None:
        (build_dir / "homelab-zfs-receive-only.sh").write_text(
            build_zfs_push_target_wrapper(),
            encoding="utf-8",
        )
        (build_dir / "zfs-push-datasets.conf").write_text(
            "\n".join(push_target_access.datasets) + "\n",
            encoding="utf-8",
        )
        (build_dir / "zfs-push-authorized-keys").write_text(
            build_zfs_push_target_authorized_keys(push_target_access),
            encoding="utf-8",
        )
        file_specs.extend(
            [
                FileSpec(
                    "homelab-zfs-receive-only.sh",
                    "/usr/local/sbin/homelab-zfs-receive-only",
                    mode="755",
                ),
                FileSpec("zfs-push-datasets.conf", "/etc/homelab/zfs-push-datasets.conf"),
                FileSpec(
                    "zfs-push-authorized-keys",
                    "/var/lib/homelab-zfs-push/.ssh/authorized_keys",
                    mode="600",
                ),
            ]
        )

    render_file(
        templates_dir / "homelab-zfs-scrub.sh",
        build_dir / "homelab-zfs-scrub.sh",
        ZFS_POOLS_BLOCK=shell_array_block("ZFS_POOLS", pools),
    )
    render_file(
        templates_dir / "zfs-scrub.service",
        build_dir / "zfs-scrub.service",
    )
    render_file(
        templates_dir / "homelab-zfs-health-check.service",
        build_dir / "homelab-zfs-health-check.service",
    )
    render_file(
        templates_dir / "homelab-zfs-health-check.timer",
        build_dir / "homelab-zfs-health-check.timer",
        HEALTH_CHECK_SCHEDULE=health_check_schedule,
    )
    (build_dir / "homelab-zfs-health-check.sh").write_text(
        build_health_check_script(pools),
        encoding="utf-8",
    )

    write_env_file(
        build_dir / "env",
        {
            "HOMELAB_STATE_DIR": homelab_state_dir,
            "ENABLE_ZFS_SNAPSHOTS": "true" if snapshot_plans and manage_snapshots else "false",
            "ENABLE_ZFS_REPLICATION": (
                "true" if replication_jobs and manage_replication else "false"
            ),
            "ENABLE_ZFS_SCRUB": "true" if pools and manage_scrub else "false",
            "ENABLE_ZFS_HEALTH_CHECK": "true" if pools and manage_health_check else "false",
            "ENABLE_ZFS_PULL_SOURCE": "true" if pull_source_access is not None else "false",
            "ZFS_PULL_SOURCE_USER": pull_source_access.user if pull_source_access else "zfs-pull",
            "ZFS_PULL_SOURCE_HOME": "/var/lib/homelab-zfs-pull",
            "ENABLE_ZFS_PUSH_TARGET": "true" if push_target_access is not None else "false",
            "ZFS_PUSH_TARGET_USER": push_target_access.user if push_target_access else "zfs-push",
            "ZFS_PUSH_TARGET_HOME": "/var/lib/homelab-zfs-push",
        },
    )

    artifacts = HostArtifacts(
        build_dir=build_dir,
        file_specs=tuple(file_specs),
    )
    write_file_map(build_dir, artifacts)
    return artifacts
