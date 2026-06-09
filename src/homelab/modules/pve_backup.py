from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .. import backup_excludes, op_secrets
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files
from . import pbs_client_backup

REMOTE_ROOT = "/tmp/homelab-pve-backup"
TMPFS_BASE = Path("/dev/shm")


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-backup")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-backup (not applicable to {requested_host})")
        return 0
    validate(root, supported_hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    config_dir = root / "pve-backup" / "configs"
    for name in ["pbs-tokens.env.example"]:
        if not (config_dir / name).is_file():
            raise ValueError(f"missing config file: {config_dir / name}")
    for host in hosts:
        validate_standalone_backup_config(root, host)


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
        username = str(storage.get("username", "")).strip()
        password_var = str(storage.get("password_var", "")).strip()
        if password_var == "PBS_BACKUP_XUR_CINCI_PASSWORD" and "!" not in username:
            raise ValueError(
                f"{host}: Xur Cinci PBS storage {name!r} must use an API token username"
            )

    seen_jobs: set[tuple[str, str, str, str]] = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"pve-backup.pbs_setup.jobs[{index}] must be a mapping for {host}")
        storage = str(job.get("storage", "")).strip()
        schedule = str(job.get("schedule", "")).strip()
        vmid = str(job.get("vmid", "")).strip()
        exclude = str(job.get("exclude", "")).strip()
        if storage not in storage_names:
            raise ValueError(
                f"{host}: backup job {index} references unknown storage {storage!r}"
            )
        key = (storage, schedule, vmid, exclude)
        if key in seen_jobs:
            raise ValueError(f"{host}: duplicate PVE backup job for storage/schedule/vmid/exclude {key}")
        seen_jobs.add(key)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "pve":
        raise ValueError(
            f"Unsupported host type for {host}: {registry.get(host, 'config.type')}"
        )
    build_dir = root / "pve-backup" / "build" / host
    prepare_build_dir(build_dir)
    build_standalone_backup_plans(root, host, build_dir)
    build_prepared_lxc_restore_plan(root, host, build_dir)
    build_config_restore_plan(root, host, build_dir)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        print_sub(
            "Standalone backup subfeature: enabled"
            if (build_dir / "storage-plan.conf").is_file()
            or (build_dir / "jobs-plan.conf").is_file()
            else "Standalone backup subfeature: disabled"
        )
        print_sub(
            "Config restore plan: enabled"
            if (build_dir / "restore-plan.conf").is_file()
            else "Config restore plan: disabled"
        )
        print_sub(
            "Prepared LXC restore plan: enabled"
            if (build_dir / "restore-ct-plan.conf").is_file()
            else "Prepared LXC restore plan: disabled"
        )
        return

    upload_paths = [
        (build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (root / "pve-backup" / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]
    token_tmpdir = None
    try:
        if (build_dir / "storage-plan.conf").is_file():
            tmp_parent = TMPFS_BASE if TMPFS_BASE.is_dir() else None
            token_tmpdir = tempfile.TemporaryDirectory(
                prefix="homelab-pve-backup.",
                dir=str(tmp_parent) if tmp_parent else None,
            )
            tokens_path = Path(token_tmpdir.name) / "pbs-tokens.env"
            write_pbs_tokens_file(root, host, tokens_path)
            upload_paths.append(
                (tokens_path, f"{REMOTE_ROOT}/build/{host}/pbs-tokens.env")
            )

        connection = HostConnection(host)
        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            upload_paths,
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib"),
        )
    finally:
        if token_tmpdir is not None:
            token_tmpdir.cleanup()


def normalize_storage_name(name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    return normalized


def shell_quote(value: object) -> str:
    return str(value).replace("'", "'\"'\"'")


def normalize_string_list(value: object, message: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(message)
    normalized = []
    for item in value:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


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


def build_standalone_backup_plans(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    storages = registry.get(host, "pve-backup.pbs_setup.storages", [])
    jobs = registry.get(host, "pve-backup.pbs_setup.jobs", [])
    if not storages and not jobs:
        return
    storage_lines = [f"STORAGE_COUNT='{len(storages)}'"]
    for index, storage in enumerate(storages):
        for required in ["name", "server", "datastore", "username"]:
            if not storage.get(required):
                raise ValueError(
                    f"Invalid standalone storage entry at index {index} for {host}"
                )
        fingerprint = storage.get("fingerprint") or read_pbs_fingerprint(
            root,
            str(storage["name"]),
        )
        password_var = storage.get("password_var") or (
            f"PBS_{normalize_storage_name(storage['name'])}_PASSWORD"
        )
        storage_lines.extend([
            f"STORAGE_{index}_NAME='{shell_quote(storage['name'])}'",
            f"STORAGE_{index}_SERVER='{shell_quote(storage['server'])}'",
            f"STORAGE_{index}_DATASTORE='{shell_quote(storage['datastore'])}'",
            f"STORAGE_{index}_USERNAME='{shell_quote(storage['username'])}'",
            f"STORAGE_{index}_FINGERPRINT='{shell_quote(fingerprint)}'",
            f"STORAGE_{index}_PASSWORD_VAR='{shell_quote(password_var)}'",
        ])
    (build_dir / "storage-plan.conf").write_text(
        "\n".join(storage_lines) + "\n",
        encoding="utf-8",
    )

    job_lines = [f"JOB_COUNT='{len(jobs)}'"]
    for index, job in enumerate(jobs):
        if not job.get("schedule") or not job.get("storage"):
            raise ValueError(
                f"Invalid standalone backup job at index {index} for {host}"
            )
        has_vmid = bool(job.get("vmid"))
        has_exclude = bool(job.get("exclude"))
        if has_vmid and has_exclude:
            raise ValueError(
                "Standalone backup job at index "
                f"{index} for {host} cannot set both vmid and exclude"
            )
        defaults = {
            "vmid": "",
            "exclude": "",
            "exclude_path": [],
            "compress": "zstd",
            "mode": "snapshot",
            "notes_template": "{{guestname}}",
            "notification_mode": "notification-system",
            "prune_backups": "keep-all=1",
            "enabled": "1",
            "fleecing": "0",
        }
        merged = {**defaults, **job}
        exclude_paths = normalize_string_list(
            merged.get("exclude_path", []),
            f"exclude_path for standalone backup job at index {index} for {host} must be a list",
        )
        exclude_profiles = backup_excludes.normalize_profile_names(
            merged.get("exclude_profiles", []),
            "exclude_profiles for standalone backup job at index "
            f"{index} for {host} must be a list",
        )
        exclude_paths = [
            *backup_excludes.load_profiles(root, exclude_profiles),
            *exclude_paths,
        ]
        mount_profiles = merged.get("mount_exclude_profiles", {})
        if mount_profiles in (None, ""):
            mount_profiles = {}
        if not isinstance(mount_profiles, dict):
            raise ValueError(
                "mount_exclude_profiles for standalone backup job at index "
                f"{index} for {host} must be a mapping"
            )
        for mountpoint, profiles_value in mount_profiles.items():
            mountpoint_text = str(mountpoint).strip()
            if not mountpoint_text:
                continue
            profiles = backup_excludes.normalize_profile_names(
                profiles_value,
                "mount_exclude_profiles entries for standalone backup job at index "
                f"{index} for {host} must be lists",
            )
            for entry in backup_excludes.load_profiles(root, profiles):
                exclude_paths.append(backup_excludes.join_mount_prefix(mountpoint_text, entry))
        exclude_paths = backup_excludes.dedupe_preserve_order(exclude_paths)
        for key in [
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
        ]:
            job_lines.append(f"JOB_{index}_{key.upper()}='{shell_quote(merged[key])}'")
        job_lines.append(f"JOB_{index}_EXCLUDE_PATH_COUNT='{len(exclude_paths)}'")
        for path_index, exclude_path in enumerate(exclude_paths):
            job_lines.append(
                f"JOB_{index}_EXCLUDE_PATH_{path_index}='{shell_quote(exclude_path)}'"
            )
    (build_dir / "jobs-plan.conf").write_text(
        "\n".join(job_lines) + "\n",
        encoding="utf-8",
    )


def build_prepared_lxc_restore_plan(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    entries = registry.get(host, "pve-backup.restore_prepared_lxcs", [])
    if not entries:
        return
    if not isinstance(entries, list):
        raise ValueError(f"pve-backup.restore_prepared_lxcs must be a list for {host}")

    lines = [f"RESTORE_CT_COUNT='{len(entries)}'"]
    for index, entry in enumerate(entries):
        for required in ["vmid", "storage", "target_storage"]:
            if not entry.get(required):
                raise ValueError(
                    f"Invalid prepared LXC restore entry at index {index} for {host}"
                )
        vmid = str(entry["vmid"])
        if not re.fullmatch(r"[1-9][0-9]{0,8}", vmid):
            raise ValueError(
                f"Invalid prepared LXC VMID at index {index} for {host}: {vmid}"
            )
        root_authorized_keys = normalize_string_list(
            entry.get("root_authorized_keys", []),
            "root_authorized_keys for prepared LXC restore entry "
            f"{index} for {host} must be a list",
        )
        start_enabled = normalize_bool(
            entry.get("start", False),
            False,
            f"restore_prepared_lxcs[{index}].start must be boolean for {host}",
        )
        ignore_unpack_errors = normalize_bool(
            entry.get("ignore_unpack_errors", False),
            False,
            f"restore_prepared_lxcs[{index}].ignore_unpack_errors must be boolean for {host}",
        )
        lines.extend([
            f"RESTORE_CT_{index}_VMID='{shell_quote(vmid)}'",
            f"RESTORE_CT_{index}_STORAGE='{shell_quote(entry['storage'])}'",
            f"RESTORE_CT_{index}_TARGET_STORAGE='{shell_quote(entry['target_storage'])}'",
            f"RESTORE_CT_{index}_UNPRIVILEGED='{shell_quote(entry.get('unprivileged', ''))}'",
            f"RESTORE_CT_{index}_IGNORE_UNPACK_ERRORS='{str(ignore_unpack_errors).lower()}'",
            f"RESTORE_CT_{index}_START='{str(start_enabled).lower()}'",
            f"RESTORE_CT_{index}_ROOT_AUTHORIZED_KEY_COUNT='{len(root_authorized_keys)}'",
        ])
        for key_index, public_key in enumerate(root_authorized_keys):
            lines.append(
                f"RESTORE_CT_{index}_ROOT_AUTHORIZED_KEY_{key_index}='{shell_quote(public_key)}'"
            )
    (build_dir / "restore-ct-plan.conf").write_text(
        "\n".join(lines) + "\n",
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
    ceph_enabled = "true" if any(
        archive.path == "/etc/ceph" for archive in plan.archives
    ) else "false"
    restore_lxc_configs = registry.get(host, "pve-backup.restore_lxc_configs", {})
    if restore_lxc_configs in (None, ""):
        restore_lxc_configs = {}
    if not isinstance(restore_lxc_configs, dict):
        raise ValueError(f"pve-backup.restore_lxc_configs must be a mapping for {host}")
    restore_lxc_enabled = normalize_bool(
        restore_lxc_configs.get("enabled", False),
        False,
        f"pve-backup.restore_lxc_configs.enabled must be boolean for {host}",
    )
    restore_lxc_autostart = normalize_bool(
        restore_lxc_configs.get("autostart", False),
        False,
        f"pve-backup.restore_lxc_configs.autostart must be boolean for {host}",
    )
    restore_lxc_vmids = normalize_string_list(
        restore_lxc_configs.get("vmids", []),
        f"pve-backup.restore_lxc_configs.vmids must be a list for {host}",
    )
    for vmid in restore_lxc_vmids:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", vmid):
            raise ValueError(
                f"Invalid LXC VMID in pve-backup.restore_lxc_configs.vmids for {host}: {vmid}"
            )
    env_source = pbs_client_backup.secret_path(root, plan.secret_profile)
    (build_dir / "pbs.env").write_text(
        env_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (build_dir / "restore-plan.conf").write_text(
        "\n".join(
            [
                f"REPOSITORY='{shell_quote(plan.repository)}'",
                f"NAMESPACE='{shell_quote(plan.namespace)}'",
                f"BACKUP_ID='{shell_quote(plan.backup_id)}'",
                f"ARCHIVE_NAME='{shell_quote(pve_archive.name)}'",
                f"CEPH_ENABLED='{shell_quote(ceph_enabled)}'",
                f"RESTORE_LXC_CONFIGS_ENABLED='{str(restore_lxc_enabled).lower()}'",
                f"RESTORE_LXC_AUTOSTART='{str(restore_lxc_autostart).lower()}'",
                f"RESTORE_LXC_CONFIG_COUNT='{len(restore_lxc_vmids)}'",
                *[
                    f"RESTORE_LXC_CONFIG_{index}_VMID='{shell_quote(vmid)}'"
                    for index, vmid in enumerate(restore_lxc_vmids)
                ],
                "",
            ]
        ),
        encoding="utf-8",
    )
