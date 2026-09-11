from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import backup_excludes, op_secrets
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    ENCRYPTION_KEY_SECRET as _ENCRYPTION_KEY_SECRET,
)
from ..module_support import (
    copy_cached_secret,
    normalize_bool,
    normalize_string_list,
    registry_has_encrypted_pve_storage,
    run_module_deploy,
    stage_encryption_keyfile,
    tmpfs_secret_stage,
    validate_secret_reference,
)
from ..output import print_sub
from ..ssh import HostConnection, build_files
from . import pbs_client_backup

REMOTE_ROOT = "/tmp/homelab-pve-backup"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        "pve-backup",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda supported_hosts, _hosts: validate(root, supported_hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    config_dir = root / "pve-backup" / "configs"
    for name in ["pbs-tokens.env.example"]:
        if not (config_dir / name).is_file():
            raise ValueError(f"missing config file: {config_dir / name}")
    for host in hosts:
        validate_standalone_backup_config(root, host)
        if host_has_encrypted_storage(root, host):
            validate_secret_reference(root, _ENCRYPTION_KEY_SECRET)


def collect_storage_names(storages: list, existing_storages: list[str], host: str) -> set[str]:
    """Every storage ID this host will have, declared plus pre-existing.

    Duplicates are rejected because PVE keys storage by ID: a repeat would silently
    overwrite the earlier definition rather than add a second target.
    """
    storage_names: set[str] = set(existing_storages)
    for index, storage in enumerate(storages):
        if not isinstance(storage, dict):
            raise ValueError(f"pve-backup.pbs_setup.storages[{index}] must be a mapping for {host}")
        name = str(storage.get("name", "")).strip()
        if not name:
            raise ValueError(f"pve-backup.pbs_setup.storages[{index}].name required for {host}")
        if name in storage_names:
            raise ValueError(f"duplicate PVE backup storage {name!r} for {host}")
        storage_names.add(name)
    return storage_names


def validate_backup_jobs(jobs: list, storage_names: set[str], host: str) -> None:
    """Every job must target a storage that exists and must not duplicate another.

    Two identical jobs would run the same backup twice on the same schedule,
    doubling load and datastore usage without any visible error.
    """
    seen_jobs: set[tuple[str, str, str, str]] = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"pve-backup.pbs_setup.jobs[{index}] must be a mapping for {host}")
        storage = str(job.get("storage", "")).strip()
        if storage not in storage_names:
            raise ValueError(f"{host}: backup job {index} references unknown storage {storage!r}")
        key = (
            storage,
            str(job.get("schedule", "")).strip(),
            str(job.get("vmid", "")).strip(),
            str(job.get("exclude", "")).strip(),
        )
        if key in seen_jobs:
            raise ValueError(
                f"{host}: duplicate PVE backup job for storage/schedule/vmid/exclude {key}"
            )
        seen_jobs.add(key)


def validate_standalone_backup_config(root: Path, host: str) -> None:
    registry = default_registry(root)
    storages = registry.get(host, "pve-backup.pbs_setup.storages", [])
    existing_storages = normalize_string_list(
        registry.get(host, "pve-backup.pbs_setup.existing_storages", []),
        f"pve-backup.pbs_setup.existing_storages must be a list for {host}",
    )
    jobs = registry.get(host, "pve-backup.pbs_setup.jobs", [])
    if not storages and not jobs:
        return
    if not isinstance(storages, list):
        raise ValueError(f"pve-backup.pbs_setup.storages must be a list for {host}")
    if not isinstance(jobs, list):
        raise ValueError(f"pve-backup.pbs_setup.jobs must be a list for {host}")

    validate_backup_jobs(jobs, collect_storage_names(storages, existing_storages, host), host)


def report_dry_run(build_dir: Path, host: str) -> None:
    """Print the build contents and which of the two subfeatures the plans enabled.

    Both lines are reported even when disabled: "no standalone backup here" is a
    deliberate inventory state on most nodes, and silence would not distinguish it
    from a plan that failed to render.
    """
    print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
    print_sub("Build files:")
    for file_name in build_files(build_dir):
        print_sub(f"    {file_name}")
    standalone = (build_dir / "storage-plan.conf").is_file() or (
        build_dir / "jobs-plan.conf"
    ).is_file()
    print_sub(f"Standalone backup subfeature: {'enabled' if standalone else 'disabled'}")
    restore = (build_dir / "restore-plan.conf").is_file()
    print_sub(f"Config restore plan: {'enabled' if restore else 'disabled'}")


def stage_secret_uploads(
    root: Path,
    host: str,
    build_dir: Path,
    secret_dir: Path,
) -> list[tuple[Path, str]]:
    """Render this host's PBS credentials into `secret_dir` and return their uploads.

    Both pbs-tokens.env and pbs-<n>.env hold live PBS credentials, and the encryption
    keyfile is the only thing standing between an offsite datastore and plaintext.
    None of them is rendered into build/ — a persistent, mode-0644 directory in the
    repo working tree. They exist only under the caller's tmpfs stage, which shreds
    them on teardown.

    Which secrets are needed follows from what the plan builders actually emitted, so
    a host with neither subfeature configured stages nothing at all.
    """
    uploads: list[tuple[Path, str]] = []

    if (build_dir / "storage-plan.conf").is_file():
        tokens_path = secret_dir / "pbs-tokens.env"
        write_pbs_tokens_file(root, host, tokens_path)
        uploads.append((tokens_path, f"{REMOTE_ROOT}/build/{host}/pbs-tokens.env"))
        if host_has_encrypted_storage(root, host):
            keyfile_path = stage_encryption_keyfile(root, secret_dir / "pbs-encryption.key")
            uploads.append((keyfile_path, f"{REMOTE_ROOT}/build/{host}/pbs-encryption.key"))

    if (build_dir / "restore-plan.conf").is_file():
        plan = pbs_client_backup.normalize_backup_plan(root, default_registry(root), host)
        for index, destination in enumerate(pbs_client_backup.destinations_for(plan)):
            uploads.append((
                copy_cached_secret(
                    root,
                    pbs_client_backup.secret_name_for_profile(destination.secret_profile),
                    secret_dir / f"pbs-{index}.env",
                ),
                f"{REMOTE_ROOT}/build/{host}/pbs-{index}.env",
            ))

    return uploads


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "pve":
        raise ValueError(
            f"Unsupported host type for {host}: {registry.get(host, 'config.type')}"
        )
    build_dir = root / "pve-backup" / "build" / host
    prepare_build_dir(build_dir)
    build_standalone_backup_plans(root, host, build_dir)
    build_config_restore_plan(root, host, build_dir)

    if dry_run:
        report_dry_run(build_dir, host)
        return

    with tmpfs_secret_stage("homelab-pve-backup.") as secret_dir:
        upload_paths = [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-backup" / "scripts", f"{REMOTE_ROOT}/scripts"),
            *stage_secret_uploads(root, host, build_dir, secret_dir),
        ]
        stage_and_run_remote_installer(
            root,
            HostConnection(host),
            REMOTE_ROOT,
            upload_paths,
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib"),
        )


def normalize_storage_name(name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    return normalized


def shell_quote(value: object) -> str:
    return str(value).replace("'", "'\"'\"'")


REQUIRED_STORAGE_KEYS = ["name", "server", "datastore", "username"]

JOB_DEFAULTS = {
    "vmid": "",
    "exclude": "",
    "compress": "zstd",
    "mode": "snapshot",
    "notes_template": "{{guestname}}",
    "notification_mode": "notification-system",
    "prune_backups": "keep-all=1",
    "enabled": "1",
    "fleecing": "0",
}

JOB_PLAN_KEYS = [
    "schedule",
    "storage",
    "vmid",
    "exclude",
    "compress",
    "mode",
    "notes_template",
    "notification_mode",
    "prune_backups",
    "enabled",
    "fleecing",
]


def storage_plan_lines(root: Path, storage: dict, index: int, host: str) -> list[str]:
    """The `STORAGE_<i>_*` block for one PBS storage.

    `fingerprint` and `password_var` fall back to the rendered `pbs-<name>` secret
    and the derived variable name respectively, so inventory only has to carry them
    when they differ from the convention.
    """
    for required in REQUIRED_STORAGE_KEYS:
        if not storage.get(required):
            raise ValueError(f"Invalid standalone storage entry at index {index} for {host}")
    fingerprint = storage.get("fingerprint") or read_pbs_fingerprint(root, str(storage["name"]))
    password_var = storage.get("password_var") or (
        f"PBS_{normalize_storage_name(storage['name'])}_PASSWORD"
    )
    encryption = normalize_bool(
        storage.get("encryption", False),
        False,
        f"pve-backup.pbs_setup.storages[{index}].encryption must be boolean for {host}",
    )
    return [
        f"STORAGE_{index}_NAME='{shell_quote(storage['name'])}'",
        f"STORAGE_{index}_SERVER='{shell_quote(storage['server'])}'",
        f"STORAGE_{index}_DATASTORE='{shell_quote(storage['datastore'])}'",
        f"STORAGE_{index}_NAMESPACE='{shell_quote(storage.get('namespace', ''))}'",
        f"STORAGE_{index}_USERNAME='{shell_quote(storage['username'])}'",
        f"STORAGE_{index}_FINGERPRINT='{shell_quote(fingerprint)}'",
        f"STORAGE_{index}_PASSWORD_VAR='{shell_quote(password_var)}'",
        f"STORAGE_{index}_ENCRYPTION='{str(encryption).lower()}'",
    ]


def mount_prefixed_excludes(root: Path, merged: dict, index: int, host: str) -> list[str]:
    """Exclude entries from `mount_exclude_profiles`, each rewritten under its mountpoint.

    A profile listed here is relative to the mountpoint rather than to /, which is
    what `join_mount_prefix` applies.
    """
    mount_profiles = merged.get("mount_exclude_profiles", {})
    if mount_profiles in (None, ""):
        mount_profiles = {}
    if not isinstance(mount_profiles, dict):
        raise ValueError(
            "mount_exclude_profiles for standalone backup job at index "
            f"{index} for {host} must be a mapping"
        )

    paths: list[str] = []
    for mountpoint, profiles_value in mount_profiles.items():
        mountpoint_text = str(mountpoint).strip()
        if not mountpoint_text:
            continue
        profiles = backup_excludes.normalize_profile_names(
            profiles_value,
            "mount_exclude_profiles entries for standalone backup job at index "
            f"{index} for {host} must be lists",
        )
        paths.extend(
            backup_excludes.join_mount_prefix(mountpoint_text, entry)
            for entry in backup_excludes.load_profiles(root, profiles)
        )
    return paths


def job_exclude_paths(root: Path, merged: dict, index: int, host: str) -> list[str]:
    """Every exclude path for one job: named profiles, then literals, then mount-scoped.

    Order is load-bearing only in that it is what the installer writes out; the
    final dedupe keeps the first occurrence of each path.
    """
    explicit = normalize_string_list(
        merged.get("exclude_path", []),
        f"exclude_path for standalone backup job at index {index} for {host} must be a list",
    )
    exclude_profiles = backup_excludes.normalize_profile_names(
        merged.get("exclude_profiles", []),
        f"exclude_profiles for standalone backup job at index {index} for {host} must be a list",
    )
    return backup_excludes.dedupe_preserve_order(
        [
            *backup_excludes.load_profiles(root, exclude_profiles),
            *explicit,
            *mount_prefixed_excludes(root, merged, index, host),
        ]
    )


def job_plan_lines(root: Path, job: dict, index: int, host: str) -> list[str]:
    """The `JOB_<i>_*` block for one backup job."""
    if not job.get("schedule") or not job.get("storage"):
        raise ValueError(f"Invalid standalone backup job at index {index} for {host}")
    if job.get("vmid") and job.get("exclude"):
        raise ValueError(
            "Standalone backup job at index "
            f"{index} for {host} cannot set both vmid and exclude"
        )
    merged = {**JOB_DEFAULTS, **job}
    exclude_paths = job_exclude_paths(root, merged, index, host)
    lines = [f"JOB_{index}_{key.upper()}='{shell_quote(merged[key])}'" for key in JOB_PLAN_KEYS]
    lines.append(f"JOB_{index}_EXCLUDE_PATH_COUNT='{len(exclude_paths)}'")
    lines.extend(
        f"JOB_{index}_EXCLUDE_PATH_{path_index}='{shell_quote(exclude_path)}'"
        for path_index, exclude_path in enumerate(exclude_paths)
    )
    return lines


def build_standalone_backup_plans(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    storages = registry.get(host, "pve-backup.pbs_setup.storages", [])
    jobs = registry.get(host, "pve-backup.pbs_setup.jobs", [])
    if not storages and not jobs:
        return

    storage_lines = [f"STORAGE_COUNT='{len(storages)}'"]
    for index, storage in enumerate(storages):
        storage_lines.extend(storage_plan_lines(root, storage, index, host))
    (build_dir / "storage-plan.conf").write_text(
        "\n".join(storage_lines) + "\n",
        encoding="utf-8",
    )

    job_lines = [f"JOB_COUNT='{len(jobs)}'"]
    for index, job in enumerate(jobs):
        job_lines.extend(job_plan_lines(root, job, index, host))
    (build_dir / "jobs-plan.conf").write_text(
        "\n".join(job_lines) + "\n",
        encoding="utf-8",
    )


def read_pbs_fingerprint(root: Path, storage_name: str) -> str:
    secret_name = f"pbs-{storage_name}"
    path = op_secrets.secret_file(root, secret_name)
    env = op_secrets.parse_env_file(path)
    fingerprint = env.get("PBS_FINGERPRINT", "").strip()
    if not fingerprint:
        raise op_secrets.OpSecretsError(
            f"PBS_FINGERPRINT is empty in rendered secret '{secret_name}'"
        )
    return fingerprint


def secret_name_for_pbs_password_var(password_var: str) -> str:
    if not password_var.startswith("PBS_") or not password_var.endswith("_PASSWORD"):
        raise op_secrets.OpSecretsError(
            f"cannot infer secret name for PBS password variable '{password_var}'"
        )
    profile = password_var.removeprefix("PBS_").removesuffix("_PASSWORD")
    return f"pbs-{profile.lower().replace('_', '-')}"


def read_pbs_password(root: Path, password_var: str) -> str:
    secret_name = secret_name_for_pbs_password_var(password_var)
    path = op_secrets.secret_file(root, secret_name)
    env = op_secrets.parse_env_file(path)
    password = env.get("PBS_PASSWORD", "").strip()
    if not password:
        raise op_secrets.OpSecretsError(
            f"PBS_PASSWORD is empty in rendered secret '{secret_name}'"
        )
    return password


def host_has_encrypted_storage(root: Path, host: str) -> bool:
    return registry_has_encrypted_pve_storage(default_registry(root), host)


def write_pbs_tokens_file(root: Path, host: str, destination: Path) -> None:
    registry = default_registry(root)
    storages = registry.get(host, "pve-backup.pbs_setup.storages", [])
    lines = [
        "# PBS storage passwords",
        "# Generated by pve-backup deploy from 1Password-backed secrets.",
        "",
    ]
    seen: set[str] = set()
    for storage in storages:
        password_var = storage.get("password_var") or (
            f"PBS_{normalize_storage_name(storage['name'])}_PASSWORD"
        )
        if password_var in seen:
            continue
        seen.add(password_var)
        lines.append(
            f"{password_var}='{shell_quote(read_pbs_password(root, password_var))}'"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination.chmod(0o600)


@dataclass(frozen=True)
class RestoreLxcConfigs:
    """The `restore_lxc_configs` block, validated."""

    enabled: bool
    autostart: bool
    vmids: list[str]


def normalize_restore_lxc_configs(registry, host: str) -> RestoreLxcConfigs:
    """Read and validate `pve-backup.restore_lxc_configs`.

    VMIDs are pattern-checked because they are interpolated into the restore
    script; anything but a plain positive integer is rejected outright.
    """
    restore_lxc_configs = registry.get(host, "pve-backup.restore_lxc_configs", {})
    if restore_lxc_configs in (None, ""):
        restore_lxc_configs = {}
    if not isinstance(restore_lxc_configs, dict):
        raise ValueError(f"pve-backup.restore_lxc_configs must be a mapping for {host}")

    vmids = normalize_string_list(
        restore_lxc_configs.get("vmids", []),
        f"pve-backup.restore_lxc_configs.vmids must be a list for {host}",
    )
    for vmid in vmids:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", vmid):
            raise ValueError(
                f"Invalid LXC VMID in pve-backup.restore_lxc_configs.vmids for {host}: {vmid}"
            )
    return RestoreLxcConfigs(
        enabled=normalize_bool(
            restore_lxc_configs.get("enabled", False),
            False,
            f"pve-backup.restore_lxc_configs.enabled must be boolean for {host}",
        ),
        autostart=normalize_bool(
            restore_lxc_configs.get("autostart", False),
            False,
            f"pve-backup.restore_lxc_configs.autostart must be boolean for {host}",
        ),
        vmids=vmids,
    )


def restore_plan_lines(plan, pve_archive, restore_lxc: RestoreLxcConfigs) -> list[str]:
    """The rendered `restore-plan.conf` body."""
    destinations = pbs_client_backup.destinations_for(plan)
    return [
        f"NAMESPACE='{shell_quote(plan.namespace)}'",
        f"BACKUP_ID='{shell_quote(plan.backup_id)}'",
        f"ARCHIVE_NAME='{shell_quote(pve_archive.name)}'",
        f"ENCRYPT='{str(plan.encrypt).lower()}'",
        f"KEYFILE='{shell_quote(pbs_client_backup.KEYFILE_REMOTE_PATH)}'",
        f"RESTORE_LXC_CONFIGS_ENABLED='{str(restore_lxc.enabled).lower()}'",
        f"RESTORE_LXC_AUTOSTART='{str(restore_lxc.autostart).lower()}'",
        f"RESTORE_LXC_CONFIG_COUNT='{len(restore_lxc.vmids)}'",
        f"DESTINATION_COUNT='{len(destinations)}'",
        *[
            f"DESTINATION_{index}_REPOSITORY='{shell_quote(destination.repository)}'"
            for index, destination in enumerate(destinations)
        ],
        *[
            f"RESTORE_LXC_CONFIG_{index}_VMID='{shell_quote(vmid)}'"
            for index, vmid in enumerate(restore_lxc.vmids)
        ],
        "",
    ]


def build_config_restore_plan(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    if not registry.has(host, "pbs-client-backup"):
        return
    plan = pbs_client_backup.normalize_backup_plan(root, registry, host)
    if not plan.enabled:
        return
    pve_archive = next(
        (archive for archive in plan.archives if archive.path == "/etc/pve"),
        None,
    )
    if pve_archive is None:
        return
    # pbs.env holds a live PBS password and is NOT written into build/; it is staged
    # from the tmpfs secret cache at upload time (see deploy_host).
    (build_dir / "restore-plan.conf").write_text(
        "\n".join(
            restore_plan_lines(plan, pve_archive, normalize_restore_lxc_configs(registry, host))
        ),
        encoding="utf-8",
    )
