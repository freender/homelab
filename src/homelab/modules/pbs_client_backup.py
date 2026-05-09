from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, require_text, write_file_map
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many, offline_mode
from ..templates import render_template

REMOTE_ROOT = "/tmp/homelab-pbs-client-backup"
MODULE_DIR = "pbs-client-backup"
SERVICE_NAME = "homelab-pbs-client-backup.service"
TIMER_NAME = "homelab-pbs-client-backup.timer"
VALID_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class ArchivePlan:
    name: str
    dataset: str
    path: str
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class BackupPlan:
    enabled: bool
    schedule: str
    repository: str
    namespace: str
    secret_profile: str
    backup_id: str
    backup_type: str
    runner: str
    docker_image: str
    docker_network: str
    archives: tuple[ArchivePlan, ...]


FILE_SPECS = (
    FileSpec("homelab-pbs-client-backup", "/usr/local/sbin/homelab-pbs-client-backup", "700"),
    FileSpec("homelab-pbs-client-backup.conf", "/etc/homelab/pbs-client-backup.conf", "600"),
    FileSpec("homelab-pbs-client-backup.env", "/etc/homelab/pbs-client-backup.env", "600"),
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
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature=MODULE_DIR)
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping {MODULE_DIR} (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / MODULE_DIR
    for path in [
        module_dir / "scripts" / "install.sh",
        module_dir / "templates" / "homelab-pbs-client-backup.sh",
        module_dir / "templates" / SERVICE_NAME,
        module_dir / "templates" / TIMER_NAME,
        module_dir / "configs" / "pbs-client-backup.env.example",
    ]:
        if not path.is_file():
            raise ValueError(f"missing required file: {path}")

    registry = default_registry(root)
    for host in hosts:
        plan = normalize_backup_plan(registry, host)
        if not plan.enabled:
            continue
        secret = secret_path(root, plan.secret_profile, allow_example=offline_mode())
        if not secret.is_file():
            expected = secret_path(root, plan.secret_profile)
            raise ValueError(f"{host}: missing secret file: {expected}")


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


def normalize_backup_plan(registry, host: str) -> BackupPlan:
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
        excludes = normalize_string_list(
            archive.get("exclude", []),
            f"archive excludes for {host} must be a list",
        )
        archives.append(
            ArchivePlan(name=name, dataset=dataset, path=path, excludes=tuple(excludes))
        )

    runner = str(registry.get(host, f"{prefix}.runner", "host")).strip().lower()
    if runner not in {"host", "docker"}:
        raise ValueError(f"{prefix}.runner for {host} must be 'host' or 'docker'")

    return BackupPlan(
        enabled=normalize_bool(
            registry.get(host, f"{prefix}.enabled", None),
            True,
            f"{prefix}.enabled for {host} must be true or false",
        ),
        schedule=str(registry.get(host, f"{prefix}.schedule", "*-*-* 00:20:00")),
        repository=require_text(
            registry.get(host, f"{prefix}.repository", ""),
            f"{prefix}.repository required for {host}",
        ),
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
        runner=runner,
        docker_image=str(registry.get(host, f"{prefix}.docker_image", "")),
        docker_network=str(registry.get(host, f"{prefix}.docker_network", "bridge")),
        archives=tuple(archives),
    )


def secret_path(root: Path, profile: str, allow_example: bool = False) -> Path:
    paths = {
        "backup-main": root / "secrets" / "pbs-backup-main.env",
        "backup-cinci": root / "secrets" / "pbs-backup-cinci.env",
        "backup-cinci-hosts": root / "secrets" / "pbs-backup-cinci-hosts.env",
        "backup-xur-cinci": root / "secrets" / "pbs-backup-xur-cinci.env",
        "backup-xur-cottonwood": root / "secrets" / "pbs-backup-xur-cottonwood.env",
    }
    if profile not in paths:
        raise ValueError(f"invalid PBS secret_profile '{profile}'")
    path = paths[profile]
    if allow_example and not path.is_file():
        return path.with_suffix(path.suffix + ".example")
    return path


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "ubuntu":
        raise ValueError(f"{MODULE_DIR} supports Ubuntu hosts only")

    plan = normalize_backup_plan(registry, host)
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
    for message in diff_many(
        connection,
        [(build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS],
    ):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy PBS client backup to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib", "scripts"),
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
    write_config(build_dir / "homelab-pbs-client-backup.conf", plan)
    secret = secret_path(root, plan.secret_profile, allow_example=offline_mode())
    (build_dir / "homelab-pbs-client-backup.env").write_text(
        secret.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    write_file_map(build_dir, FILE_SPECS)


def write_config(path: Path, plan: BackupPlan) -> None:
    lines = [
        f'REPOSITORY="{plan.repository}"',
        f'NAMESPACE="{plan.namespace}"',
        f'BACKUP_ID="{plan.backup_id}"',
        f'BACKUP_TYPE="{plan.backup_type}"',
        f'RUNNER="{plan.runner}"',
        f'DOCKER_IMAGE="{plan.docker_image}"',
        f'DOCKER_NETWORK="{plan.docker_network}"',
        f'ARCHIVE_COUNT="{len(plan.archives)}"',
    ]
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
