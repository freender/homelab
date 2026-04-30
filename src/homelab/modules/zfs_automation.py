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
    auto_exclude_replication: bool = False


@dataclass(frozen=True)
class ReplicationPlan:
    source: str
    target: str
    post_hook: str = ""


@dataclass(frozen=True)
class ReplicationJob:
    name: str
    schedule: str
    plans: tuple[ReplicationPlan, ...]
    after_commands: tuple[str, ...]
    syncoid_options: tuple[str, ...]


@dataclass(frozen=True)
class ZfsPuller:
    name: str
    from_address: str
    public_key: str


@dataclass(frozen=True)
class ZfsPullSourceAccess:
    user: str
    datasets: tuple[str, ...]
    pullers: tuple[ZfsPuller, ...]


BASE_FILE_SPECS = (
    FileSpec("sanoid.conf", "/etc/sanoid/sanoid.conf"),
    FileSpec(
        "homelab-zfs-snapshots.service",
        "/etc/systemd/system/homelab-zfs-snapshots.service",
    ),
    FileSpec("homelab-zfs-snapshots.timer", "/etc/systemd/system/homelab-zfs-snapshots.timer"),
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


def normalize_snapshot_plans(registry, host: str) -> list[SnapshotPlan]:
    explicit = registry.get(host, "zfs-automation.snapshot_plans", None)
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit:
            raise ValueError(f"zfs-automation.snapshot_plans must be a non-empty list for {host}")
        plans: list[SnapshotPlan] = []
        seen: set[str] = set()
        for index, plan in enumerate(explicit):
            if not isinstance(plan, dict):
                raise ValueError(f"invalid snapshot plan at index {index} for {host}")
            dataset = require_string(
                plan.get("dataset", ""),
                f"snapshot plan dataset required for {host}",
            )
            if dataset in seen:
                raise ValueError(f"duplicate snapshot plan dataset {dataset} for {host}")
            seen.add(dataset)
            if "exclude" in plan and "excludes" in plan:
                raise ValueError(
                    f"snapshot plan at index {index} for {host} specifies both 'exclude' and"
                    " 'excludes'; use only 'exclude'"
                )
            excludes = normalize_string_list(
                plan.get("exclude", plan.get("excludes", [])),
                f"snapshot plan excludes must be a list for {host}",
            )
            plans.append(
                SnapshotPlan(
                    dataset=dataset,
                    excludes=tuple(excludes),
                    hourly=str(plan.get("hourly", 0)),
                    daily=str(plan.get("daily", 7)),
                    weekly=str(plan.get("weekly", 4)),
                    monthly=str(plan.get("monthly", 3)),
                    yearly=str(plan.get("yearly", 0)),
                    recursive=normalize_bool(
                        plan.get("recursive"),
                        True,
                        f"recursive for snapshot plan {dataset} must be true or false for {host}",
                    ),
                    process_children_only=normalize_bool(
                        plan.get("process_children_only"),
                        True,
                        "process_children_only for snapshot plan "
                        f"{dataset} must be true or false for {host}",
                    ),
                    auto_exclude_replication=True,
                )
            )
        return plans

    zfs_pool = str(registry.get(host, "zfs-automation.sanoid.dataset", "cache"))
    excludes = registry.get(host, "zfs-automation.sanoid.exclude", [])
    if not isinstance(excludes, list):
        raise ValueError(f"zfs-automation.sanoid.exclude must be a list for {host}")
    return [
        SnapshotPlan(
            dataset=zfs_pool,
            excludes=tuple(str(item) for item in excludes),
            hourly=str(registry.get(host, "zfs-automation.sanoid.hourly", 0)),
            daily=str(registry.get(host, "zfs-automation.sanoid.daily", 7)),
            weekly=str(registry.get(host, "zfs-automation.sanoid.weekly", 4)),
            monthly=str(registry.get(host, "zfs-automation.sanoid.monthly", 3)),
            yearly=str(registry.get(host, "zfs-automation.sanoid.yearly", 0)),
            recursive=True,
            process_children_only=True,
            auto_exclude_replication=True,
        )
    ]


def normalize_replication_config(
    registry, host: str, *, include_disabled: bool = False
) -> list[ReplicationJob]:
    jobs = registry.get(host, "zfs-automation.replication_jobs", None)
    if jobs is not None:
        if not isinstance(jobs, dict):
            raise ValueError(f"zfs-automation.replication_jobs must be a dict for {host}")

        parsed_jobs: list[ReplicationJob] = []
        seen_job_names: set[str] = set()
        for job_name, job_config in jobs.items():
            if not isinstance(job_config, dict):
                raise ValueError(f"invalid replication job '{job_name}' for {host}")

            normalized_job_name = normalize_replication_job_name(job_name, host)
            if normalized_job_name in seen_job_names:
                raise ValueError(
                    f"duplicate replication job name '{normalized_job_name}' for {host}"
                )
            seen_job_names.add(normalized_job_name)

            enabled = normalize_bool(
                job_config.get("enabled"),
                True,
                f"enabled for replication job '{normalized_job_name}' must be true or false"
                f" for {host}",
            )
            if not enabled and not include_disabled:
                continue

            schedule = str(job_config.get("schedule", "*-*-* 02:30:00"))
            plans: list[ReplicationPlan] = []
            explicit_plans = job_config.get("plans", [])
            if not isinstance(explicit_plans, list):
                raise ValueError(
                    f"plans for replication job '{normalized_job_name}' must be a list for {host}"
                )

            for index, plan in enumerate(explicit_plans):
                if not isinstance(plan, dict):
                    raise ValueError(
                        f"invalid plan at index {index} in job '{normalized_job_name}' for {host}"
                    )
                plans.append(
                    ReplicationPlan(
                        source=require_string(
                            plan.get("source", ""),
                            f"plan source required at index {index} in job"
                            f" '{normalized_job_name}' for {host}",
                        ),
                        target=require_string(
                            plan.get("target", ""),
                            f"plan target required at index {index} in job"
                            f" '{normalized_job_name}' for {host}",
                        ),
                        post_hook=str(plan.get("post_hook", "")).strip(),
                    )
                )

            after_commands = normalize_string_list(
                job_config.get("after_replication_commands", []),
                f"after_replication_commands for job '{normalized_job_name}' must be a list"
                f" for {host}",
            )
            syncoid_options = normalize_string_list(
                job_config.get("syncoid_options", []),
                f"syncoid_options for job '{normalized_job_name}' must be a list for {host}",
            )

            parsed_jobs.append(
                ReplicationJob(
                    name=normalized_job_name,
                    schedule=schedule,
                    plans=tuple(plans),
                    after_commands=tuple(after_commands),
                    syncoid_options=tuple(syncoid_options),
                )
            )
        return parsed_jobs

    # Fallback to old format
    explicit = registry.get(host, "zfs-automation.replication_plans", None)
    if explicit is not None:
        if not isinstance(explicit, list):
            raise ValueError(f"zfs-automation.replication_plans must be a list for {host}")
        plans: list[ReplicationPlan] = []
        for index, plan in enumerate(explicit):
            if not isinstance(plan, dict):
                raise ValueError(f"invalid replication plan at index {index} for {host}")
            plans.append(
                ReplicationPlan(
                    source=require_string(
                        plan.get("source", ""),
                        f"replication plan source required at index {index} for {host}",
                    ),
                    target=require_string(
                        plan.get("target", ""),
                        f"replication plan target required at index {index} for {host}",
                    ),
                    post_hook=str(plan.get("post_hook", "")).strip(),
                )
            )
        after_commands = normalize_string_list(
            registry.get(host, "zfs-automation.after_replication_commands", []),
            f"zfs-automation.after_replication_commands must be a list for {host}",
        )
        schedule = str(registry.get(host, "zfs-automation.replication_schedule", "*-*-* 02:30:00"))
        return [
            ReplicationJob(
                name="default",
                schedule=schedule,
                plans=tuple(plans),
                after_commands=tuple(after_commands),
                syncoid_options=(),
            )
        ]

    replication = registry.get(host, "zfs-automation.replication", None)
    if replication is None:
        return []
    if not isinstance(replication, dict):
        raise ValueError(f"zfs-automation.replication must be a mapping for {host}")

    source = require_string(
        registry.get(host, "zfs-automation.replication.source", ""),
        f"zfs-automation.replication.source required for {host}",
    )
    target = require_string(
        registry.get(host, "zfs-automation.replication.target", ""),
        f"zfs-automation.replication.target required for {host}",
    )
    homelab_state_dir = str(registry.get(host, "config.homelab_state_dir", "/var/lib/homelab"))
    docker_restart = str(
        registry.get(
            host,
            "zfs-automation.docker_restart_command",
            f"{homelab_state_dir}/appdata/start.sh",
        )
    ).strip()
    after_commands = [docker_restart] if docker_restart else []
    schedule = str(registry.get(host, "zfs-automation.replication_schedule", "*-*-* 02:30:00"))
    return [
        ReplicationJob(
            name="default",
            schedule=schedule,
            plans=(
                ReplicationPlan(
                    source=source,
                    target=target,
                    post_hook=str(
                        registry.get(host, "zfs-automation.replication_post_hook", "")
                    ).strip(),
                ),
            ),
            after_commands=tuple(after_commands),
            syncoid_options=(),
        )
    ]


def normalize_pull_source_access(registry, host: str) -> ZfsPullSourceAccess | None:
    config = registry.get(host, "zfs-automation.pull_source_access", None)
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError(f"zfs-automation.pull_source_access must be a mapping for {host}")

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
        user=user,
        datasets=tuple(datasets),
        pullers=tuple(pullers),
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
        for dataset in (plan.source, plan.target)
        if not is_remote_dataset(dataset)
    ]
    for dataset in [*(plan.dataset for plan in snapshot_plans), *local_replication_datasets]:
        pool = dataset_pool(dataset)
        if pool not in pools:
            pools.append(pool)
    if pools:
        return pools
    return ["cache"]


def build_sanoid_config(
    snapshot_plans: list[SnapshotPlan],
    replication_jobs: list[ReplicationJob],
) -> str:
    lines: list[str] = []
    replication_datasets = {plan.target for job in replication_jobs for plan in job.plans}
    for plan in snapshot_plans:
        lines.extend(
            [
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
        )

        excluded = {
            normalize_dataset_under_root(dataset, plan.dataset) for dataset in plan.excludes
        }
        if plan.auto_exclude_replication:
            excluded.update(
                replication_exclude
                for dataset in replication_datasets
                for replication_exclude in local_replication_excludes(dataset, plan.dataset)
            )

        for dataset in sorted(excluded):
            lines.extend([f"[{dataset}]", "autosnap = no", "autoprune = no", ""])

    return "\n".join(lines).rstrip() + "\n"


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
) -> str:
    syncoid_options_block = shell_array_block("SYNCOID_OPTIONS", syncoid_options)
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        syncoid_options_block,
        "",
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
        "syncoid_sshkey() {",
        "  local option",
        "  for option in \"${SYNCOID_OPTIONS[@]}\"; do",
        "    case \"$option\" in",
        "      --sshkey=*) printf '%s\\n' \"${option#--sshkey=}\"; return 0 ;;",
        "    esac",
        "  done",
        "  return 1",
        "}",
        "",
        "list_snapshot_names() {",
        "  local dataset_ref=\"$1\"",
        "  local dataset remote sshkey",
        "",
        "  if [[ \"$dataset_ref\" == *:* ]]; then",
        "    remote=\"${dataset_ref%%:*}\"",
        "    dataset=\"${dataset_ref#*:}\"",
        "    if sshkey=\"$(syncoid_sshkey)\"; then",
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
        "  list_snapshot_names \"$source\" | sort -u > \"$source_snaps\"",
        "  list_snapshot_names \"$target\" | sort -u > \"$target_snaps\"",
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
        "  comm -12 \"$source_snaps\" \"$target_snaps\" > \"$common_snaps\"",
        "  if [[ ! -s \"$common_snaps\" ]]; then",
        "    echo \"ERROR: source $source and target $target have no common\" \\",
        "      \"snapshots; refusing destructive replication\" >&2",
        "    exit 1",
        "  fi",
        "",
        "  rm -f \"$source_snaps\" \"$target_snaps\" \"$common_snaps\"",
        "}",
        "",
        "wait_for_existing_replication",
        "",
    ]
    if not replication_plans:
        lines.extend(["echo 'No replication plans configured; nothing to do'", ""])
    else:
        for plan in replication_plans:
            command = [
                "/usr/sbin/syncoid",
                "-r",
                "--delete-target-snapshots",
                "--force-delete",
                '"${SYNCOID_OPTIONS[@]}"',
                plan.source,
                plan.target,
            ]
            lines.append(
                f"require_common_snapshot_lineage {quote(plan.source)} {quote(plan.target)}"
            )
            lines.append(
                " ".join(
                    item if item == '"${SYNCOID_OPTIONS[@]}"' else quote(item)
                    for item in command
                )
            )
            if plan.post_hook:
                lines.append(plan.post_hook)
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
        registry.get(host, "zfs-automation.snapshot_schedule", "*-*-* 04:35:00")
    )
    health_check_schedule = str(
        registry.get(host, "zfs-automation.health_check_schedule", "hourly")
    )
    manage_snapshots = bool(registry.get(host, "zfs-automation.manage_snapshots", True))
    manage_replication = bool(registry.get(host, "zfs-automation.manage_replication", True))
    manage_scrub = bool(registry.get(host, "zfs-automation.manage_scrub", True))
    manage_health_check = bool(registry.get(host, "zfs-automation.manage_health_check", True))
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
            "ENABLE_ZFS_REPLICATION": (
                "true" if replication_jobs and manage_replication else "false"
            ),
            "ENABLE_ZFS_SCRUB": "true" if pools and manage_scrub else "false",
            "ENABLE_ZFS_HEALTH_CHECK": "true" if pools and manage_health_check else "false",
            "ENABLE_ZFS_PULL_SOURCE": "true" if pull_source_access is not None else "false",
            "ZFS_PULL_SOURCE_USER": pull_source_access.user if pull_source_access else "zfs-pull",
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
