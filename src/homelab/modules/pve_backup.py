from __future__ import annotations

import re
from pathlib import Path

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, offline_mode
from ..templates import render_template

REMOTE_ROOT = "/tmp/homelab-pve-backup"


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
    template_dir = root / "pve-backup" / "templates"
    for name in ["pve-config-backup.sh.tpl", "pbs-tokens.env.example"]:
        if not (config_dir / name).is_file():
            raise ValueError(f"missing config file: {config_dir / name}")
    for name in ["homelab-pve-config-backup.service", "homelab-pve-config-backup.timer"]:
        if not (template_dir / name).is_file():
            raise ValueError(f"missing template: {template_dir / name}")
    registry = default_registry(root)
    for host in hosts:
        secret_profile = str(
            registry.get(host, "pve-backup.proxmox_backup_client.secret_profile", "")
        )
        secret_file = secret_path(root, secret_profile, allow_example=offline_mode())
        if secret_profile and not secret_file.is_file():
            raise ValueError(
                f"{host}: missing secret file: {secret_path(root, secret_profile)}"
            )


def secret_path(root: Path, profile: str, allow_example: bool = False) -> Path:
    if profile == "backup-main":
        secret = root / "secrets" / "pbs-backup-main.env"
        if allow_example and not secret.is_file():
            return root / "secrets" / "pbs-backup-main.env.example"
        return secret
    if profile == "backup-cinci":
        secret = root / "secrets" / "pbs-backup-cinci.env"
        if allow_example and not secret.is_file():
            return root / "secrets" / "pbs-backup-cinci.env.example"
        return secret
    raise ValueError(f"invalid secret_profile '{profile}'")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "pve":
        raise ValueError(
            f"Unsupported host type for {host}: {registry.get(host, 'config.type')}"
        )
    build_dir = root / "pve-backup" / "build" / host
    prepare_build_dir(build_dir)
    build_standalone_backup_plans(root, host, build_dir)
    build_cluster_config_backup_bundle(root, host, build_dir)

    connection = HostConnection(host)
    print_sub("Comparing with remote configs...")
    for local_name, remote_path in [
        ("pve-config-backup.sh", "/root/pve-config-backup.sh"),
        (
            "homelab-pve-config-backup.service",
            "/etc/systemd/system/homelab-pve-config-backup.service",
        ),
        (
            "homelab-pve-config-backup.timer",
            "/etc/systemd/system/homelab-pve-config-backup.timer",
        ),
        ("pbs.env", "/etc/homelab/pve-config-backup.env"),
    ]:
        local_path = build_dir / local_name
        if local_path.is_file():
            _, message = connection.remote_diff(local_path, remote_path)
            print_sub(message)

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
            "Cluster config backup subfeature: enabled"
            if (build_dir / "homelab-pve-config-backup.timer").is_file()
            else "Cluster config backup subfeature: disabled"
        )
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-backup" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
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


def build_standalone_backup_plans(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    storages = registry.get(host, "pve-backup.pbs_setup.storages", [])
    jobs = registry.get(host, "pve-backup.pbs_setup.jobs", [])
    if not storages and not jobs:
        return
    storage_lines = [f"STORAGE_COUNT='{len(storages)}'"]
    for index, storage in enumerate(storages):
        for required in ["name", "server", "datastore", "username", "fingerprint"]:
            if not storage.get(required):
                raise ValueError(
                    f"Invalid standalone storage entry at index {index} for {host}"
                )
        password_var = f"PBS_{normalize_storage_name(storage['name'])}_PASSWORD"
        storage_lines.extend([
            f"STORAGE_{index}_NAME='{shell_quote(storage['name'])}'",
            f"STORAGE_{index}_SERVER='{shell_quote(storage['server'])}'",
            f"STORAGE_{index}_DATASTORE='{shell_quote(storage['datastore'])}'",
            f"STORAGE_{index}_USERNAME='{shell_quote(storage['username'])}'",
            f"STORAGE_{index}_FINGERPRINT='{shell_quote(storage['fingerprint'])}'",
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
            "compress": "zstd",
            "mode": "snapshot",
            "notes_template": "{{guestname}}",
            "notification_mode": "notification-system",
            "prune_backups": "keep-all=1",
            "enabled": "1",
            "fleecing": "0",
        }
        merged = {**defaults, **job}
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
    (build_dir / "jobs-plan.conf").write_text(
        "\n".join(job_lines) + "\n",
        encoding="utf-8",
    )


def build_cluster_config_backup_bundle(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    repository = str(registry.get(host, "pve-backup.proxmox_backup_client.repository", ""))
    if not repository:
        return
    schedule = str(
        registry.get(host, "pve-backup.proxmox_backup_client.schedule", "*-*-* 00:30:00")
    )
    backup_id = str(
        registry.get(host, "pve-backup.proxmox_backup_client.backup_id", "pve-config")
    )
    archive_name = str(
        registry.get(host, "pve-backup.proxmox_backup_client.archive_name", "etc-pve")
    )
    ceph_enabled = "true" if registry.has(host, "ceph") else "false"
    profile = str(
        registry.get(host, "pve-backup.proxmox_backup_client.secret_profile", "")
    )
    if not profile:
        print_sub(
            f"proxmox_backup_client.secret_profile not set for {host}; "
            "skipping config backup bundle"
        )
        return
    env_source = secret_path(root, profile, allow_example=offline_mode())
    render_template(
        root / "pve-backup" / "configs" / "pve-config-backup.sh.tpl",
        build_dir / "pve-config-backup.sh",
        REPOSITORY=repository,
        BACKUP_ID=backup_id,
        ARCHIVE_NAME=archive_name,
        CEPH_ENABLED=ceph_enabled,
    )
    (build_dir / "pve-config-backup.sh").chmod(0o700)
    (build_dir / "homelab-pve-config-backup.service").write_text(
        (root / "pve-backup" / "templates" / "homelab-pve-config-backup.service").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    render_template(
        root / "pve-backup" / "templates" / "homelab-pve-config-backup.timer",
        build_dir / "homelab-pve-config-backup.timer",
        SCHEDULE=schedule,
    )
    (build_dir / "pbs.env").write_text(
        env_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (build_dir / "restore-plan.conf").write_text(
        "\n".join(
            [
                f"REPOSITORY='{shell_quote(repository)}'",
                f"BACKUP_ID='{shell_quote(backup_id)}'",
                f"ARCHIVE_NAME='{shell_quote(archive_name)}'",
                f"CEPH_ENABLED='{shell_quote(ceph_enabled)}'",
                "",
            ]
        ),
        encoding="utf-8",
    )
