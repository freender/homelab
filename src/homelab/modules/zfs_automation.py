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
GROUP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
STATIC_CONFIG_FILES = ["zfs-scrub.timer"]
TEMPLATE_FILES = [
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
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
    homelab_state_dir: str
    deploy_user: str
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
    require_active_lxc: int | None = None


@dataclass(frozen=True)
class MigratableLxcSnapshotPlan:
    vmid: int
    dataset: str


@dataclass(frozen=True)
class MigratableLxcSnapshotGroup:
    name: str
    plans: tuple[MigratableLxcSnapshotPlan, ...]


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
        validate_no_zfs_replication_config(registry, host)
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


def normalize_migratable_lxc_snapshot_group_name(value: object, host: str) -> str:
    name = require_string(value, f"migratable LXC snapshot group name required for {host}")
    if not GROUP_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"migratable LXC snapshot group '{name}' for {host} must use only letters, "
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


def dataset_pool(dataset: str) -> str:
    dataset_name = dataset.split(":", 1)[1] if ":" in dataset else dataset
    return dataset_name.split("/", 1)[0]


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


def normalize_migratable_lxc_snapshot_groups(
    registry,
    host: str,
) -> dict[str, MigratableLxcSnapshotGroup]:
    groups_config = registry.get(host, "zfs-automation.migratable_lxc_snapshot_groups", None)
    if groups_config is None:
        return {}
    if not isinstance(groups_config, dict):
        raise ValueError(
            f"zfs-automation.migratable_lxc_snapshot_groups must be a dict for {host}"
        )

    groups: dict[str, MigratableLxcSnapshotGroup] = {}
    for group_name, group_config in groups_config.items():
        if not isinstance(group_config, dict):
            raise ValueError(f"invalid migratable LXC snapshot group '{group_name}' for {host}")
        normalized_group_name = normalize_migratable_lxc_snapshot_group_name(group_name, host)
        if normalized_group_name in groups:
            raise ValueError(
                f"duplicate migratable LXC snapshot group '{normalized_group_name}' for {host}"
            )

        explicit_plans = group_config.get("plans", [])
        if not isinstance(explicit_plans, list) or not explicit_plans:
            raise ValueError(
                "migratable LXC snapshot group "
                f"'{normalized_group_name}' plans must be a non-empty list for {host}"
            )
        plans: list[MigratableLxcSnapshotPlan] = []
        seen_datasets: set[str] = set()
        for index, plan in enumerate(explicit_plans):
            if not isinstance(plan, dict):
                raise ValueError(
                    "invalid migratable LXC snapshot plan at index "
                    f"{index} in group '{normalized_group_name}' for {host}"
                )
            vmid = normalize_positive_int(
                plan.get("vmid"),
                "vmid must be a positive integer for migratable LXC snapshot plan "
                f"{index} in group '{normalized_group_name}' for {host}",
            )
            dataset = require_string(
                plan.get("dataset", ""),
                "dataset required for migratable LXC snapshot plan "
                f"{index} in group '{normalized_group_name}' for {host}",
            )
            if dataset in seen_datasets:
                raise ValueError(
                    f"duplicate dataset {dataset} in migratable LXC snapshot group "
                    f"'{normalized_group_name}' for {host}"
                )
            seen_datasets.add(dataset)
            plans.append(MigratableLxcSnapshotPlan(vmid=vmid, dataset=dataset))

        groups[normalized_group_name] = MigratableLxcSnapshotGroup(
            name=normalized_group_name,
            plans=tuple(plans),
        )
    return groups


def expand_migratable_lxc_snapshot_group(
    registry,
    value: object,
    defaults: dict,
    host: str,
) -> list[SnapshotPlan]:
    ref = require_string(value, f"migratable_lxc_snapshot_group must be set for {host}")
    if ":" in ref:
        source_host, group_name = ref.split(":", 1)
    elif "." in ref:
        source_host, group_name = ref.split(".", 1)
    else:
        raise ValueError(
            f"migratable_lxc_snapshot_group for {host} must use host:group format"
        )

    groups = normalize_migratable_lxc_snapshot_groups(registry, source_host)
    group = groups.get(group_name)
    if group is None:
        raise ValueError(f"migratable_lxc_snapshot_group {ref} not found for {host}")

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
                    plan.get("migratable_lxc_snapshot_group"),
                    defaults,
                    host,
                )
                if "migratable_lxc_snapshot_group" in plan
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


def validate_no_zfs_replication_config(registry, host: str) -> None:
    obsolete_keys = (
        "replication_defaults",
        "replication_jobs",
        "replication_plans",
        "replication",
        "pull_source_access",
    )
    for key in obsolete_keys:
        if registry.get(host, f"zfs-automation.{key}", None) is not None:
            raise ValueError(
                f"zfs-automation.{key} is no longer supported for {host}; "
                "use PVE replication and PBS instead"
            )


def resolve_pools(registry, host: str) -> list[str]:
    explicit = registry.get(host, "zfs-automation.pools", None)
    if explicit is not None:
        return normalize_string_list(explicit, f"zfs-automation.pools must be a list for {host}")

    snapshot_plans = normalize_snapshot_plans(registry, host)
    pools: list[str] = []
    for dataset in [plan.dataset for plan in snapshot_plans]:
        pool = dataset_pool(dataset)
        if pool not in pools:
            pools.append(pool)
    if pools:
        return pools
    return ["cache"]


def sanoid_plan_lines(plan: SnapshotPlan) -> list[str]:
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
    for dataset in sorted(excluded):
        lines.extend([f"[{dataset}]", "autosnap = no", "autoprune = no", ""])

    return lines


def build_sanoid_config(snapshot_plans: list[SnapshotPlan]) -> str:
    lines: list[str] = []
    for plan in snapshot_plans:
        lines.extend(sanoid_plan_lines(plan))

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


def build_snapshot_script(snapshot_plans: list[SnapshotPlan]) -> str:
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        'CONFIG_DIR="$(mktemp -d)"',
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
            config_lines = sanoid_plan_lines(plan)
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


def shell_array_block(name: str, values: list[str]) -> str:
    lines = [f"{name}=("]
    for value in values:
        lines.append(f"  {quote(value)}")
    lines.append(")")
    return "\n".join(lines)


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


def resolve_remote_path(spec: FileSpec, artifacts: HostArtifacts) -> str:
    return spec.remote_path.format(homelab_state_dir=artifacts.homelab_state_dir)


def write_file_map(build_dir: Path, artifacts: HostArtifacts) -> None:
    lines = [
        f"{spec.build_name}|{resolve_remote_path(spec, artifacts)}|{spec.mode}"
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
        (artifacts.build_dir / spec.build_name, resolve_remote_path(spec, artifacts))
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

    deploy_user = str(
        registry.get(host, "ubuntu-setup.deploy_user", registry.get(host, "config.user"))
    )
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

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
    (build_dir / "sanoid.conf").write_text(
        build_sanoid_config(snapshot_plans),
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
        build_snapshot_script(snapshot_plans),
        encoding="utf-8",
    )

    file_specs = list(BASE_FILE_SPECS)
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
            "DEPLOY_USER": deploy_user,
            "HOMELAB_STATE_DIR": homelab_state_dir,
            "ENABLE_ZFS_SNAPSHOTS": "true" if snapshot_plans and manage_snapshots else "false",
            "ENABLE_ZFS_REPLICATION": "false",
            "ENABLE_ZFS_SCRUB": "true" if pools and manage_scrub else "false",
            "ENABLE_ZFS_HEALTH_CHECK": "true" if pools and manage_health_check else "false",
            "ENABLE_ZFS_PULL_SOURCE": "false",
            "ZFS_PULL_SOURCE_USER": "zfs-pull",
            "ZFS_PULL_SOURCE_HOME": "/var/lib/homelab-zfs-pull",
            "REBUILD_BUNDLE_ROOT": f"{homelab_state_dir}/zfs-automation",
        },
    )

    artifacts = HostArtifacts(
        build_dir=build_dir,
        homelab_state_dir=homelab_state_dir,
        deploy_user=deploy_user,
        file_specs=tuple(file_specs),
    )
    write_file_map(build_dir, artifacts)
    return artifacts
