from __future__ import annotations

from pathlib import Path

from invoke.exceptions import UnexpectedExit

from ..build import copy_files, render_file
from ..deploy import DeploySession, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import normalize_bool, run_module_deploy
from ..output import print_sub
from ..ssh import HostConnection, build_files, offline_mode

REMOTE_ROOT = "/tmp/homelab-pve-gpu-passthrough"
REQUIRED_ROOT_TOKEN = "root=ZFS=rpool/ROOT/pve-1"
GPU_BLACKLIST_REMOTE_PATH = "/etc/modprobe.d/homelab-gpu-blacklist.conf"
VFIO_REMOTE_PATH = "/etc/modprobe.d/vfio.conf"
VFIO_MODULES_REMOTE_PATH = "/etc/modules-load.d/vfio.conf"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    _force: bool,
    session: DeploySession,
) -> int:
    def validate_and_warn(_supported_hosts: list[str], _hosts: list[str]) -> None:
        validate(root)
        print_sub("WARNING: This will modify systemd-boot cmdline, modules, and initramfs")

    return run_module_deploy(
        root,
        requested_host,
        "pve-gpu-passthrough",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run),
        validate=validate_and_warn,
    )


def validate(root: Path) -> None:
    configs_dir = root / "pve-gpu-passthrough" / "configs"
    required_files = [
        configs_dir / "blacklist.conf",
        configs_dir / "cmdline",
        configs_dir / "modules",
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
    isolate_host_gpu = normalize_isolate_host_gpu(registry, host)
    pci_ids = str(registry.get(host, "pve-gpu-passthrough.pci_ids", "")).strip()

    connection = HostConnection(host)
    if not dry_run and not dataset_exists(connection, root_dataset):
        raise ValueError(f"Required ZFS dataset not found on {host}: {root_dataset}")
    if dry_run and offline_mode():
        print_sub(f"[?] zfs dataset check skipped for {root_dataset} (offline validation)")
    elif dry_run and not dataset_exists(connection, root_dataset):
        raise ValueError(f"Required ZFS dataset not found on {host}: {root_dataset}")

    prepare_build_dir(build_dir)
    cmdline_value = build_cmdline(configs_dir / "cmdline", isolate_host_gpu)
    (build_dir / "cmdline").write_text(f"{cmdline_value}\n", encoding="utf-8")
    if isolate_host_gpu:
        copy_files(configs_dir, build_dir, ["blacklist.conf"])
    if pci_ids:
        copy_files(configs_dir, build_dir, ["modules"])
        render_file(configs_dir / "vfio.conf.tpl", build_dir / "vfio.conf", PCI_IDS=pci_ids)

    print_sub("Comparing with remote configs...")
    diff_remote_files(
        connection,
        build_dir,
        manage_blacklist=isolate_host_gpu,
        manage_vfio=bool(pci_ids),
    )

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_install(root, host, build_dir, connection)


def normalize_isolate_host_gpu(registry, host: str) -> bool:
    return normalize_bool(
        registry.get(host, "pve-gpu-passthrough.isolate_host_gpu", None),
        False,
        f"pve-gpu-passthrough.isolate_host_gpu must be true or false for {host}",
    )


def dataset_exists(connection: HostConnection, dataset: str) -> bool:
    try:
        connection.connection.run(
            f"zfs list -H -o name '{dataset}' >/dev/null 2>&1",
            hide=True,
        )
    except UnexpectedExit:
        return False
    return True


def build_cmdline(cmdline_path: Path, isolate_host_gpu: bool) -> str:
    base_cmdline = cmdline_path.read_text(encoding="utf-8").splitlines()[0].strip()
    if isolate_host_gpu:
        return f"{base_cmdline} video=efifb:off"
    return base_cmdline


def diff_remote_files(
    connection: HostConnection,
    build_dir: Path,
    *,
    manage_blacklist: bool,
    manage_vfio: bool,
) -> None:
    remote_map = [(build_dir / "cmdline", "/etc/kernel/cmdline")]
    if manage_blacklist:
        remote_map.append((build_dir / "blacklist.conf", GPU_BLACKLIST_REMOTE_PATH))
    else:
        print_sub(f"[-] {GPU_BLACKLIST_REMOTE_PATH} (will be removed if present)")
    if manage_vfio:
        remote_map.append((build_dir / "vfio.conf", VFIO_REMOTE_PATH))
        remote_map.append((build_dir / "modules", VFIO_MODULES_REMOTE_PATH))
    else:
        print_sub(f"[-] {VFIO_REMOTE_PATH} (will be removed if present)")
        print_sub(f"[-] {VFIO_MODULES_REMOTE_PATH} (will be removed if present)")

    for local_path, remote_path in remote_map:
        _, message = connection.remote_diff(local_path, remote_path)
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
