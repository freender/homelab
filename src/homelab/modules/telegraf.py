from __future__ import annotations

import shutil
from pathlib import Path

from ..deploy import DeploySession, prepare_build_dir
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files

REMOTE_ROOT = "/tmp/homelab-telegraf"
COMMON_CONFS = ["sensors.conf", "smartctl.conf", "diskio.conf", "disk.conf", "net.conf", "mem.conf"]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="telegraf")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping telegraf (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    registry = default_registry(root)
    common_dir = root / "telegraf" / "configs" / "common"
    for conf in ["telegraf.conf", *COMMON_CONFS]:
        if not (common_dir / conf).is_file():
            raise ValueError(f"Missing {common_dir / conf}")
    apc_config = root / "telegraf" / "configs" / "roles" / "apc" / "apcupsd.conf"
    for host in hosts:
        role = str(registry.get(host, "apcupsd.role", "none"))
        if role in {"master", "master-standalone"} and not apc_config.is_file():
            raise ValueError(f"APC config not found for master node {host}: {apc_config}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    build_dir = root / "telegraf" / "build" / host
    common_dir = root / "telegraf" / "configs" / "common"
    config_root = root / "telegraf" / "configs"
    prepare_build_dir(build_dir)
    telegraf_d = build_dir / "telegraf.d"
    telegraf_d.mkdir(parents=True, exist_ok=True)

    shutil.copy2(common_dir / "telegraf.conf", build_dir / "telegraf.conf")
    for conf in COMMON_CONFS:
        shutil.copy2(common_dir / conf, telegraf_d / conf)

    role = str(registry.get(host, "apcupsd.role", "none"))
    if role in {"master", "master-standalone"}:
        shutil.copy2(config_root / "roles" / "apc" / "apcupsd.conf", telegraf_d / "apcupsd.conf")
    shutil.copy2(config_root / "roles" / "zfs" / "zfs.conf", telegraf_d / "zfs.conf")
    sudoers = common_dir / "telegraf-smartctl-sudoers"
    if sudoers.is_file():
        shutil.copy2(sudoers, build_dir / "telegraf-smartctl-sudoers")

    host_dir = config_root / host
    if host_dir.is_dir():
        for conf in sorted(host_dir.glob("*.conf")):
            shutil.copy2(conf, telegraf_d / conf.name)

    connection = HostConnection(host)
    print_sub(f"Comparing with {host}:/etc/telegraf...")
    for file_path in sorted(build_dir.rglob("*")):
        if not file_path.is_file() or file_path.name == "telegraf-smartctl-sudoers":
            continue
        remote_path = "/etc/telegraf/" + str(file_path.relative_to(build_dir)).replace("\\", "/")
        _, message = connection.remote_diff(file_path, remote_path)
        print_sub(message)
    if (build_dir / "telegraf-smartctl-sudoers").is_file():
        _, message = connection.remote_diff(
            build_dir / "telegraf-smartctl-sudoers",
            "/etc/sudoers.d/telegraf-smartctl",
        )
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    print_sub("Staging bundle...")
    connection.prepare_remote_dir(REMOTE_ROOT, "build", "lib")
    connection.upload_paths([
        (build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (root / "telegraf" / "scripts", f"{REMOTE_ROOT}/scripts"),
    ])
    connection.upload_shared_libs(root, REMOTE_ROOT)
    print_sub("Running installer...")
    connection.run_remote_installer(
        REMOTE_ROOT,
        "scripts/install.sh",
        host,
        env={"FORCE_UPDATE": "true" if force else "false"},
        require_root=True,
    )
