from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import backup_excludes, op_secrets
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    ENCRYPTION_KEY_SECRET,
    FileSpec,
    copy_cached_secret,
    feature_paused,
    normalize_bool,
    normalize_string_list,
    registry_has_encrypted_pve_storage,
    require_text,
    run_module_deploy,
    stage_encryption_keyfile,
    tmpfs_secret_stage,
    validate_secret_reference,
    write_file_map,
)
from ..output import print_sub
from ..ssh import HostConnection, build_files, diff_many
from ..templates import render_template

REMOTE_ROOT = "/tmp/homelab-pbs-client-backup"
MODULE_DIR = "pbs-client-backup"
SERVICE_NAME = "homelab-pbs-client-backup.service"
TIMER_NAME = "homelab-pbs-client-backup.timer"
# On-host location of the shared PBS client-side encryption keyfile. The same
# path is used by the restore side (pve-backup config restore) so encrypted
# /etc/pve archives can be decrypted.
KEYFILE_REMOTE_PATH = "/etc/homelab/pbs-encryption.key"
VALID_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PROFILE_TO_SECRET = {
    "backup-main": "pbs-backup-main",
    "backup-cinci": "pbs-backup-cinci",
    "host-backup-cinci": "pbs-host-backup-cinci",
    "host-backup-cottonwood": "pbs-host-backup-cottonwood",
    "backup-xur-cinci": "pbs-backup-xur-cinci",
    "backup-xur-cottonwood": "pbs-backup-xur-cottonwood",
    "backup-osiris-cottonwood": "pbs-backup-osiris-cottonwood",
}


@dataclass(frozen=True)
class ArchivePlan:
    name: str
    dataset: str
    path: str
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class BackupDestination:
    repository: str
    secret_profile: str


@dataclass(frozen=True)
class BackupPlan:
    enabled: bool
    paused: bool
    schedule: str
    repository: str
    namespace: str
    secret_profile: str
    backup_id: str
    backup_type: str
    host_type: str
    encrypt: bool
    purge_keyfile: bool
    archives: tuple[ArchivePlan, ...]
    fallback_destinations: tuple[BackupDestination, ...]


FILE_SPECS = (
    FileSpec("homelab-pbs-client-backup", "/usr/local/sbin/homelab-pbs-client-backup", "700"),
    FileSpec("homelab-pbs-client-backup.conf", "/etc/homelab/pbs-client-backup.conf", "600"),
    FileSpec(SERVICE_NAME, f"/etc/systemd/system/{SERVICE_NAME}"),
    FileSpec(TIMER_NAME, f"/etc/systemd/system/{TIMER_NAME}"),
)


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
        MODULE_DIR,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / MODULE_DIR
    for path in [
        module_dir / "scripts" / "install.sh",
        module_dir / "templates" / "homelab-pbs-client-backup.sh",
        module_dir / "templates" / SERVICE_NAME,
        module_dir / "templates" / TIMER_NAME,
        module_dir / "configs" / "pbs-client-backup.env.example",
        module_dir / "configs" / "keyrings" / "proxmox-release-trixie.gpg",
    ]:
        if not path.is_file():
            raise ValueError(f"missing required file: {path}")

    registry = default_registry(root)
    for host in hosts:
        plan = normalize_backup_plan(root, registry, host)
        if not plan.enabled:
            continue
        for destination in destinations_for(plan):
            try:
                secret = secret_path(root, destination.secret_profile)
            except (ValueError, op_secrets.OpSecretsError) as exc:
                raise ValueError(f"{host}: {exc}") from exc
            if not secret.is_file():
                raise ValueError(
                    f"{host}: missing secret file for profile '{destination.secret_profile}'"
                )
        if plan.encrypt:
            try:
                validate_secret_reference(root, ENCRYPTION_KEY_SECRET)
            except op_secrets.OpSecretsError as exc:
                raise ValueError(f"{host}: {exc}") from exc


def normalize_backup_plan(root: Path, registry, host: str) -> BackupPlan:
    prefix = MODULE_DIR
    archives_config = registry.get(host, f"{prefix}.archives", [])
    if not isinstance(archives_config, list) or not archives_config:
        raise ValueError(f"{prefix}.archives must be a non-empty list for {host}")

    archives: list[ArchivePlan] = []
    seen: set[str] = set()
    for index, archive in enumerate(archives_config):
        if not isinstance(archive, dict):
            raise ValueError(f"invalid {prefix}.archives entry at index {index} for {host}")
        name = require_text(archive.get("name", ""), f"archive name required for {host}")
        if not VALID_ARCHIVE_NAME.fullmatch(name):
            raise ValueError(
                f"archive name {name!r} for {host} must use letters, numbers, "
                "dot, dash, or underscore"
            )
        if name in seen:
            raise ValueError(f"duplicate archive name {name!r} for {host}")
        seen.add(name)
        dataset = str(archive.get("dataset", "")).strip()
        path = str(archive.get("path", "")).strip()
        if bool(dataset) == bool(path):
            raise ValueError(
                f"archive {name!r} for {host} must specify exactly one of dataset or path"
            )
        exclude_profiles = backup_excludes.normalize_profile_names(
            archive.get("exclude_profiles", []),
            f"archive exclude_profiles for {host} must be a list",
        )
        excludes = normalize_string_list(
            archive.get("exclude", []),
            f"archive excludes for {host} must be a list",
        )
        excludes = backup_excludes.dedupe_preserve_order([
            *backup_excludes.load_profiles(root, exclude_profiles),
            *excludes,
        ])
        archives.append(
            ArchivePlan(name=name, dataset=dataset, path=path, excludes=tuple(excludes))
        )

    host_type = str(registry.get(host, "config.type", "")).strip().lower()
    if host_type not in {"ubuntu", "pve"}:
        raise ValueError(
            f"{prefix} for {host} requires config.type of 'ubuntu' or 'pve'"
        )

    fallback_config = registry.get(host, f"{prefix}.fallback_destinations", [])
    if not isinstance(fallback_config, list):
        raise ValueError(f"{prefix}.fallback_destinations must be a list for {host}")
    fallback_destinations: list[BackupDestination] = []
    for index, destination in enumerate(fallback_config):
        if not isinstance(destination, dict):
            raise ValueError(
                f"invalid {prefix}.fallback_destinations entry at index {index} for {host}"
            )
        fallback_destinations.append(
            BackupDestination(
                repository=require_text(
                    destination.get("repository", ""),
                    f"fallback repository required for {host}",
                ),
                secret_profile=require_text(
                    destination.get("secret_profile", ""),
                    f"fallback secret_profile required for {host}",
                ),
            )
        )

    repository = require_text(
        registry.get(host, f"{prefix}.repository", ""),
        f"{prefix}.repository required for {host}",
    )
    if any(destination.repository == repository for destination in fallback_destinations):
        raise ValueError(f"duplicate PBS repository for {host}")

    encrypt = normalize_bool(
        registry.get(host, f"{prefix}.encrypt", None),
        False,
        f"{prefix}.encrypt for {host} must be true or false",
    )
    # When client archives are unencrypted, remove the shared keyfile from the
    # host so it only exists where it is actually used. Never purge on a host
    # whose pve-backup storages are encrypted: guest vzdump and /etc/pve
    # restores read the same path.
    purge_keyfile = not encrypt and not registry_has_encrypted_pve_storage(registry, host)

    return BackupPlan(
        enabled=normalize_bool(
            registry.get(host, f"{prefix}.enabled", None),
            True,
            f"{prefix}.enabled for {host} must be true or false",
        ),
        paused=feature_paused(registry, host, prefix),
        schedule=str(registry.get(host, f"{prefix}.schedule", "*-*-* 00:20:00")),
        repository=repository,
        namespace=str(registry.get(host, f"{prefix}.namespace", "")).strip(),
        secret_profile=require_text(
            registry.get(host, f"{prefix}.secret_profile", ""),
            f"{prefix}.secret_profile required for {host}",
        ),
        backup_id=require_text(
            registry.get(host, f"{prefix}.backup_id", host),
            f"{prefix}.backup_id required for {host}",
        ),
        backup_type=str(registry.get(host, f"{prefix}.backup_type", "host")),
        host_type=host_type,
        encrypt=encrypt,
        purge_keyfile=purge_keyfile,
        archives=tuple(archives),
        fallback_destinations=tuple(fallback_destinations),
    )


def secret_path(root: Path, profile: str) -> Path:
    return op_secrets.secret_file(root, secret_name_for_profile(profile))


def secret_name_for_profile(profile: str) -> str:
    if profile not in PROFILE_TO_SECRET:
        raise ValueError(f"invalid PBS secret_profile '{profile}'")
    return PROFILE_TO_SECRET[profile]


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    plan = normalize_backup_plan(root, registry, host)
    if not plan.enabled:
        print_sub(f"{MODULE_DIR} disabled for {host}; skipping")
        return

    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)
    build_host_bundle(root, host, plan, build_dir)

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname", host)),
    )
    print_sub("Comparing with remote configs...")
    destinations = destinations_for(plan)
    for message in diff_many(
        connection,
        [
            (build_dir / spec.build_name, spec.remote_path)
            for spec in FILE_SPECS
        ] + [
            (secret_path(root, destination.secret_profile), destination_env_path(index))
            for index, destination in enumerate(destinations)
        ],
    ):
        print_sub(message)

    if dry_run:
        if plan.paused:
            print_sub(
                f"[DRY-RUN] Would pause PBS client backup on {host} "
                "(stop and disable the timer)"
            )
        print_sub(f"[DRY-RUN] Would deploy PBS client backup to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    with tmpfs_secret_stage("homelab-pbs-client-backup.") as secret_dir:
        upload_paths = [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / MODULE_DIR / "configs", f"{REMOTE_ROOT}/configs"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ]
        for index, destination in enumerate(destinations):
            upload_paths.append((
                copy_cached_secret(
                    root,
                    secret_name_for_profile(destination.secret_profile),
                    secret_dir / f"destination-{index}.env",
                ),
                f"{REMOTE_ROOT}/build/{host}/destination-{index}.env",
            ))
        if plan.encrypt:
            keyfile_stage = stage_encryption_keyfile(
                root,
                secret_dir / "pbs-encryption.key",
            )
            upload_paths.append(
                (keyfile_stage, f"{REMOTE_ROOT}/build/{host}/pbs-encryption.key")
            )
        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            upload_paths,
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "configs", "lib", "scripts"),
        )


def build_host_bundle(root: Path, host: str, plan: BackupPlan, build_dir: Path) -> None:
    render_template(
        root / MODULE_DIR / "templates" / "homelab-pbs-client-backup.sh",
        build_dir / "homelab-pbs-client-backup",
    )
    (build_dir / "homelab-pbs-client-backup").chmod(0o700)
    render_template(
        root / MODULE_DIR / "templates" / SERVICE_NAME,
        build_dir / SERVICE_NAME,
    )
    render_template(
        root / MODULE_DIR / "templates" / TIMER_NAME,
        build_dir / TIMER_NAME,
        SCHEDULE=plan.schedule,
    )
    write_config(
        build_dir / "homelab-pbs-client-backup.conf",
        plan,
    )
    write_file_map(build_dir, FILE_SPECS)


def write_config(path: Path, plan: BackupPlan) -> None:
    lines = [
        f'NAMESPACE="{plan.namespace}"',
        f'BACKUP_ID="{plan.backup_id}"',
        f'BACKUP_TYPE="{plan.backup_type}"',
        f'HOST_TYPE="{plan.host_type}"',
        f'PAUSED="{str(plan.paused).lower()}"',
        f'ENCRYPT="{str(plan.encrypt).lower()}"',
        f'PURGE_KEYFILE="{str(plan.purge_keyfile).lower()}"',
        f'KEYFILE="{KEYFILE_REMOTE_PATH}"',
        f'RETIRE_PVE_CONFIG_BACKUP="{str(plan.host_type == "pve").lower()}"',
        f'ARCHIVE_COUNT="{len(plan.archives)}"',
        f'DESTINATION_COUNT="{len(destinations_for(plan))}"',
    ]
    for index, destination in enumerate(destinations_for(plan)):
        lines.extend([
            f'DESTINATION_{index}_REPOSITORY="{destination.repository}"',
            f'DESTINATION_{index}_ENV_FILE="{destination_env_path(index)}"',
        ])
    for index, archive in enumerate(plan.archives):
        lines.extend(
            [
                f'ARCHIVE_{index}_NAME="{archive.name}"',
                f'ARCHIVE_{index}_DATASET="{archive.dataset}"',
                f'ARCHIVE_{index}_PATH="{archive.path}"',
                f'ARCHIVE_{index}_EXCLUDE_COUNT="{len(archive.excludes)}"',
            ]
        )
        for exclude_index, exclude in enumerate(archive.excludes):
            lines.append(f'ARCHIVE_{index}_EXCLUDE_{exclude_index}="{exclude}"')
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")


def destinations_for(plan: BackupPlan) -> tuple[BackupDestination, ...]:
    return (BackupDestination(plan.repository, plan.secret_profile), *plan.fallback_destinations)


def destination_env_path(index: int) -> str:
    return f"/etc/homelab/pbs-client-backup-destination-{index}.env"
