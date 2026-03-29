from __future__ import annotations

from pathlib import Path

from invoke.exceptions import UnexpectedExit

from ..build import copy_files, render_file
from ..deploy import DeploySession, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files

MODULE_NAME = "GPU Passthrough Configs"
REMOTE_ROOT = "/tmp/homelab-pve-gpu-passthrough"
REQUIRED_ROOT_TOKEN = "root=ZFS=rpool/ROOT/pve-1"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    del force

    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-gpu-passthrough")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-gpu-passthrough (not applicable to {requested_host})")
        return 0

    try:
        validate(root)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    print_sub("WARNING: This will modify systemd-boot cmdline, modules, and initramfs")
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    configs_dir = root / "pve-gpu-passthrough" / "configs"
    required_files = [
        configs_dir / "modules",
        configs_dir / "blacklist.conf",
        configs_dir / "cmdline",
        configs_dir / "vfio.conf.tpl",
    ]
    for file_path in required_files:
        if not file_path.is_file():
            raise ValueError(f"required file not found: {file_path}")

    cmdline_value = (configs_dir / "cmdline").read_text(encoding="utf-8").splitlines()[0]
    if REQUIRED_ROOT_TOKEN not in cmdline_value:
        raise ValueError(
            "Unsafe cmdline in pve-gpu-passthrough/configs/cmdline; "
            f"missing required token: {REQUIRED_ROOT_TOKEN}"
        )


def deploy_host(root: Path, host: str, dry_run: bool) -> None:
    registry = default_registry(root)
    module_dir = root / "pve-gpu-passthrough"
    configs_dir = module_dir / "configs"
    build_dir = module_dir / "build" / host
    root_dataset = REQUIRED_ROOT_TOKEN.removeprefix("root=ZFS=")

    try:
        pci_ids = registry.get(host, "pve-gpu-passthrough.pci_ids")
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    connection = HostConnection(host)
    if not dataset_exists(connection, root_dataset):
        raise ValueError(f"Required ZFS dataset not found on {host}: {root_dataset}")

    prepare_build_dir(build_dir)
    copy_files(configs_dir, build_dir, ["blacklist.conf", "cmdline", "modules"])
    render_file(configs_dir / "vfio.conf.tpl", build_dir / "vfio.conf", PCI_IDS=pci_ids)

    print_sub("Comparing with remote configs...")
    diff_remote_files(connection, build_dir)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_install(root, host, build_dir, connection)


def dataset_exists(connection: HostConnection, dataset: str) -> bool:
    try:
        connection.connection.run(
            f"zfs list -H -o name '{dataset}' >/dev/null 2>&1",
            hide=True,
        )
    except UnexpectedExit:
        return False
    return True


def diff_remote_files(connection: HostConnection, build_dir: Path) -> None:
    remote_map = {
        "blacklist.conf": "/etc/modprobe.d/blacklist.conf",
        "vfio.conf": "/etc/modprobe.d/vfio.conf",
        "modules": "/etc/modules-load.d/vfio.conf",
        "cmdline": "/etc/kernel/cmdline",
    }
    for local_name, remote_path in remote_map.items():
        _, message = connection.remote_diff(build_dir / local_name, remote_path)
        print_sub(message)


def stage_and_install(root: Path, host: str, build_dir: Path, connection: HostConnection) -> None:
    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-gpu-passthrough" / "scripts", f"{REMOTE_ROOT}/scripts"),
            (root / "pve-gpu-passthrough" / "remove.sh", f"{REMOTE_ROOT}/remove.sh"),
        ],
        "scripts/install.sh",
        host,
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
