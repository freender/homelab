"""Replication job normalization: expand and validate hosts.conf replication config.

Builds `ReplicationJob`/`ReplicationPlan` objects on top of `.normalize`'s
generic helpers and dynamic-source resolution.
"""

from __future__ import annotations

from .normalize import (
    expand_migratable_lxc_replication_plans,
    normalize_bool,
    normalize_dynamic_lxc_source,
    normalize_dynamic_lxc_source_from_candidates,
    normalize_replication_job_name,
    normalize_string_list,
    normalize_target_snapshot_prune,
    parse_migratable_lxc_group_ref,
    require_string,
)
from .types import ReplicationJob, ReplicationPlan


def resolve_replication_job_template(
    registry,
    value: object,
    host: str,
    job_name: str,
) -> dict:
    source_host, template_name = parse_migratable_lxc_group_ref(
        value,
        host,
        f"template for replication job '{job_name}'",
    )
    templates = registry.get(source_host, "zfs-automation.replication_job_templates", None)
    if not isinstance(templates, dict):
        raise ValueError(
            f"zfs-automation.replication_job_templates must be a dict for {source_host}"
        )
    template = templates.get(template_name)
    if not isinstance(template, dict):
        raise ValueError(
            f"replication job template {source_host}:{template_name} not found for {host}"
        )
    return dict(template)


def expand_replication_job_config(registry, host: str, job_name: str, job_config: dict) -> dict:
    template_ref = job_config.get("template", job_config.get("replication_job_template"))
    if template_ref is None:
        return job_config
    config = resolve_replication_job_template(registry, template_ref, host, job_name)
    config.update(
        (key, value)
        for key, value in job_config.items()
        if key not in {"template", "replication_job_template"}
    )
    return config


def resolve_replication_job_set(
    registry,
    value: object,
    host: str,
    job_name: object,
) -> list[object]:
    source_host, set_name = parse_migratable_lxc_group_ref(
        value,
        host,
        f"replication job set '{job_name}'",
    )
    job_sets = registry.get(source_host, "zfs-automation.replication_job_sets", None)
    if not isinstance(job_sets, dict):
        raise ValueError(f"zfs-automation.replication_job_sets must be a dict for {source_host}")
    refs = job_sets.get(set_name)
    if not isinstance(refs, list):
        raise ValueError(f"replication job set {source_host}:{set_name} must be a list for {host}")
    return refs


def expand_replication_jobs(registry, host: str, jobs: dict) -> list[tuple[str, dict]]:
    expanded_jobs: list[tuple[str, dict]] = []
    for job_name, job_config in jobs.items():
        if isinstance(job_config, dict):
            expanded_jobs.append((str(job_name), job_config))
            continue
        job_refs = resolve_replication_job_set(registry, job_config, host, job_name)
        for index, job_ref in enumerate(job_refs):
            if isinstance(job_ref, str):
                _, template_name = parse_migratable_lxc_group_ref(
                    job_ref,
                    host,
                    f"replication job set entry for '{job_name}'",
                )
                expanded_jobs.append((template_name, {"template": job_ref}))
                continue
            if not isinstance(job_ref, dict):
                raise ValueError(
                    f"invalid replication job set entry at index {index} for '{job_name}' on {host}"
                )
            name = normalize_replication_job_name(
                job_ref.get("name", ""),
                host,
            )
            template_ref = require_string(
                job_ref.get("template", job_ref.get("replication_job_template", "")),
                f"template required for replication job set entry '{name}' on {host}",
            )
            config = {"template": template_ref}
            config.update(
                (key, value)
                for key, value in job_ref.items()
                if key not in {"name", "template", "replication_job_template"}
            )
            expanded_jobs.append((name, config))
    return expanded_jobs


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
    for job_name, job_config in expand_replication_jobs(registry, host, jobs):
        normalized_job_name = normalize_replication_job_name(job_name, host)
        job_config = expand_replication_job_config(registry, host, normalized_job_name, job_config)
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

        # `paused: true` keeps the job fully deployed (unit files stay installed)
        # but stops and disables its timer so it does not run. This differs from
        # `enabled: false`, which retires the job entirely (units removed). A
        # paused job stays in the returned list so its units are still managed.
        paused = normalize_bool(
            job_config.get("paused"),
            False,
            f"paused for replication job '{normalized_job_name}' must be true or false for {host}",
        )

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
                paused=paused,
            )
        )
    return parsed_jobs


