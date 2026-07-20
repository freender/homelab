from __future__ import annotations

import shutil
from pathlib import Path

from .. import op_secrets
from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, copy_cached_secret, tmpfs_secret_stage, write_file_map
from ..output import print_action, print_sub, print_warn
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-lxc-docker-hooks"
FEATURE = "pve-lxc-docker-hooks"
HOOK_NAME = "homelab-docker-bbolt-sync-hook.sh"
MONITOR_NAME = "homelab-docker-bbolt-monitor.sh"
SERVICE_NAME = "homelab-docker-bbolt-monitor"
BASE_FILE_SPECS = (
    FileSpec(HOOK_NAME, f"/var/lib/vz/snippets/{HOOK_NAME}", "755"),
    FileSpec("bbolt", "/usr/local/bin/bbolt", "755"),
    FileSpec(MONITOR_NAME, f"/usr/local/sbin/{MONITOR_NAME}", "755"),
    FileSpec(f"{SERVICE_NAME}.service", f"/etc/systemd/system/{SERVICE_NAME}.service"),
    FileSpec(f"{SERVICE_NAME}.timer", f"/etc/systemd/system/{SERVICE_NAME}.timer"),
)
TELEGRAM_FILE_SPEC = FileSpec("telegram.env", "/etc/homelab/telegram.env", "600")


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature=FEATURE)
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping {FEATURE} (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    installer = root / "pve-lxc-docker-hooks" / "scripts" / "install.sh"
    hook = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-sync-hook.sh"
    monitor = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.sh"
    monitor_svc = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.service"
    monitor_timer = root / "pve-lxc-docker-hooks" / "scripts" / "homelab-docker-bbolt-monitor.timer"
    bbolt = root / "pve-lxc-docker-hooks" / "configs" / "bbolt"
    for path in (installer, hook, monitor, monitor_svc, monitor_timer, bbolt):
        if not path.is_file():
            raise ValueError(f"missing required file: {path}")

    registry = default_registry(root)
    for host in hosts:
        vmids = registry.get(host, f"{FEATURE}.vmids", [])
        if not isinstance(vmids, list) or not vmids:
            raise ValueError(f"{FEATURE}.vmids must be a non-empty list for {host}")
        for vmid in vmids:
            if not isinstance(vmid, int):
                raise ValueError(f"{FEATURE}.vmids entries must be integers for {host}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )
    vmids = registry.get(host, f"{FEATURE}.vmids")

    build_dir = root / "pve-lxc-docker-hooks" / "build" / host
    prepare_build_dir(build_dir)
    module_dir = root / "pve-lxc-docker-hooks"
    write_env_file(
        build_dir / "env",
        {
            "DOCKER_LXC_VMIDS": " ".join(str(vmid) for vmid in vmids),
        },
    )
    shutil.copy2(module_dir / "scripts" / HOOK_NAME, build_dir / HOOK_NAME)
    shutil.copy2(module_dir / "configs" / "bbolt", build_dir / "bbolt")
    shutil.copy2(module_dir / "scripts" / MONITOR_NAME, build_dir / MONITOR_NAME)
    shutil.copy2(
        module_dir / "scripts" / f"{SERVICE_NAME}.service",
        build_dir / f"{SERVICE_NAME}.service",
    )
    shutil.copy2(
        module_dir / "scripts" / f"{SERVICE_NAME}.timer",
        build_dir / f"{SERVICE_NAME}.timer",
    )

    telegram_src: Path | None = None
    try:
        telegram_src = op_secrets.secret_file(root, "telegram")
    except op_secrets.OpSecretsError:
        print_warn(f"{host}: telegram secret unavailable; Telegram notifications will be disabled")

    file_specs = BASE_FILE_SPECS
    if telegram_src is not None:
        file_specs = (*file_specs, TELEGRAM_FILE_SPEC)
    write_file_map(build_dir, file_specs)

    diff_pairs = []
    for spec in file_specs:
        source = telegram_src if spec.build_name == "telegram.env" else build_dir / spec.build_name
        if source is not None:
            diff_pairs.append((source, spec.remote_path))
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if dry_run:
        print_action(f"[DRY-RUN] Would deploy {FEATURE} to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    if telegram_src is None:
        upload_paths = [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ]
        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            upload_paths,
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            interpreter="bash",
            remote_subdirs=("build", "lib"),
        )
        return

    with tmpfs_secret_stage("homelab-pve-lxc-docker-hooks.") as secret_dir:
        secret_path = copy_cached_secret(root, "telegram", secret_dir / "telegram.env")
        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            [
                (build_dir, f"{REMOTE_ROOT}/build/{host}"),
                (secret_path, f"{REMOTE_ROOT}/build/{host}/telegram.env"),
                (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
            ],
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            interpreter="bash",
            remote_subdirs=("build", "lib"),
        )
