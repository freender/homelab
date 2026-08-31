"""Replication job normalization: expand and validate hosts.conf replication config.

Builds `ReplicationJob`/`ReplicationPlan` objects on top of `.normalize`'s
generic helpers.
"""

from __future__ import annotations

from .normalize import (
    expand_migratable_lxc_replication_plans,
    normalize_bool,
    normalize_replication_job_name,
    normalize_string_list,
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

        schedule = str(job_config.get("schedule", "*-*-* 02:30:00"))
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
            for index, plan in enumerate(explicit_plans):
                if not isinstance(plan, dict):
                    raise ValueError(
                        f"invalid plan at index {index} in job '{normalized_job_name}' for {host}"
                    )
                source = str(plan.get("source", "")).strip()
                if not source:
                    raise ValueError(
                        f"plan at index {index} in job '{normalized_job_name}' for {host} "
                        "must specify source"
                    )
                plans.append(
                    ReplicationPlan(
                        target=require_string(
                            plan.get("target", ""),
                            f"plan target required at index {index} in job"
                            f" '{normalized_job_name}' for {host}",
                        ),
                        source=source,
                    )
                )

        syncoid_options = normalize_string_list(
            job_config.get("syncoid_options", []),
            f"syncoid_options for job '{normalized_job_name}' must be a list for {host}",
        )
        delete_target_snapshots = normalize_bool(
            job_config.get("delete_target_snapshots"),
            True,
            "delete_target_snapshots for replication job "
            f"'{normalized_job_name}' must be true or false for {host}",
        )
        parsed_jobs.append(
            ReplicationJob(
                name=normalized_job_name,
                schedule=schedule,
                plans=tuple(plans),
                syncoid_options=tuple(syncoid_options),
                delete_target_snapshots=delete_target_snapshots,
                paused=paused,
            )
        )
    return parsed_jobs
