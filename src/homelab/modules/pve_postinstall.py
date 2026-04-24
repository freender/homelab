from __future__ import annotations

from pathlib import Path

from ..build import copy_file, copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..media_storage import load_media_storage
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-postinstall"
PVE_FILES = [
    "proxmox.sources",
    "ceph.sources",
    "pve-test.sources",
    "no-nag-script",
    "pve-remove-nag.sh",
    "sshd-hardening.conf",
    "notify-failure.sh",
    "homelab-notify-failure@.service",
]

REMOTE_PATHS = {
    "proxmox.sources": "/etc/apt/sources.list.d/proxmox.sources",
    "ceph.sources": "/etc/apt/sources.list.d/ceph.sources",
    "pve-test.sources": "/etc/apt/sources.list.d/pve-test.sources",
    "no-nag-script": "/etc/apt/apt.conf.d/no-nag-script",
    "pve-remove-nag.sh": "/usr/local/bin/pve-remove-nag.sh",
    "sshd-hardening.conf": "/etc/ssh/sshd_config.d/99-disable-password-auth.conf",
    "notify-failure.sh": "/usr/local/bin/homelab-notify-failure",
    "homelab-notify-failure@.service": "/etc/systemd/system/homelab-notify-failure@.service",
}

MODES = {
    "pve-remove-nag.sh": "755",
    "notify-failure.sh": "755",
}


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-postinstall")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-postinstall (not applicable to {requested_host})")
        return 0

    try:
        validate(root)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    config_dir = root / "pve-postinstall" / "configs" / "pve"
    interfaces_template = root / "pve-postinstall" / "templates" / "pve-interfaces"
    notify_script = root / "ubuntu-setup" / "scripts" / "notify-failure.sh"
    notify_template = root / "ubuntu-setup" / "templates" / "homelab-notify-failure@.service"

    for file_name in PVE_FILES:
        if file_name in {"notify-failure.sh", "homelab-notify-failure@.service"}:
            continue
        file_path = config_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing config file: {file_path}")

    if not interfaces_template.is_file():
        raise ValueError(f"missing interfaces template: {interfaces_template}")
    if not notify_script.is_file():
        raise ValueError(f"missing notify script: {notify_script}")
    if not notify_template.is_file():
        raise ValueError(f"missing notify template: {notify_template}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        host_type = registry.get(host, "config.type")
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    timezone = str(registry.get(host, "pve-postinstall.timezone", "UTC"))
    import_pools_raw = registry.get(host, "pve-postinstall.import_pools", [])
    if not isinstance(import_pools_raw, list):
        raise ValueError(f"pve-postinstall.import_pools must be a list for {host}")
    import_pools = " ".join(str(p) for p in import_pools_raw)

    mounts: list[str] = []
    mounts_raw = registry.get(host, "pve-postinstall.mounts", None)
    media_storage = load_media_storage(registry, host)
    if mounts_raw is None and media_storage is not None:
        mounts_raw = [
            {"label": label, "path": path}
            for label, path in media_storage.raw_mounts()
        ]
    elif mounts_raw is None:
        mounts_raw = []
    if not isinstance(mounts_raw, list):
        raise ValueError(f"pve-postinstall.mounts must be a list for {host}")
    for m in mounts_raw:
        if not isinstance(m, dict) or "label" not in m or "path" not in m:
            raise ValueError(f"pve-postinstall.mounts entry must have label and path for {host}")
        mounts.append(f"{m['label']}:{m['path']}")
    mounts_str = " ".join(mounts)

    if host_type != "pve":
        raise ValueError(f"Unsupported host type for {host}: {host_type}")

    module_dir = root / "pve-postinstall"
    config_dir = module_dir / "configs" / "pve"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    for file_name in PVE_FILES:
        if file_name in {"notify-failure.sh", "homelab-notify-failure@.service"}:
            continue
        source_path = config_dir / file_name
        if not source_path.is_file():
            raise ValueError(f"Missing config file: {source_path}")
    copy_files(
        config_dir,
        build_dir,
        [
            file_name
            for file_name in PVE_FILES
            if file_name not in {"notify-failure.sh", "homelab-notify-failure@.service"}
        ],
    )
    copy_file(
        root / "ubuntu-setup" / "scripts" / "notify-failure.sh",
        build_dir / "notify-failure.sh",
    )
    render_file(
        root / "ubuntu-setup" / "templates" / "homelab-notify-failure@.service",
        build_dir / "homelab-notify-failure@.service",
        NOTIFY_SCRIPT="/usr/local/bin/homelab-notify-failure",
    )

    write_file_map(build_dir)
    build_network_interfaces_bundle(root, host, build_dir)

    connection = HostConnection(host)
    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [(build_dir / file_name, REMOTE_PATHS[file_name]) for file_name in PVE_FILES],
    ):
        print_sub(message)

    interfaces_path = build_dir / "interfaces"
    if interfaces_path.is_file():
        _, message = connection.remote_diff(interfaces_path, "/etc/network/interfaces")
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        if interfaces_path.is_file():
            print_sub("Network interfaces subfeature: enabled")
        else:
            print_sub("Network interfaces subfeature: disabled")
        return

    stage_and_install(
        root,
        host,
        host_type,
        timezone,
        import_pools,
        mounts_str,
        build_dir,
        connection,
        force=force,
    )



def write_file_map(build_dir: Path) -> None:
    lines = [
        f"{file_name}|{REMOTE_PATHS[file_name]}|{MODES.get(file_name, '644')}"
        for file_name in PVE_FILES
    ]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_network_interfaces_bundle(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    try:
        interfaces_config = registry.get(host, "pve-postinstall.interfaces")
    except HostLookupError:
        return

    if not isinstance(interfaces_config, dict):
        return

    try:
        mgmt_ip = str(registry.get(host, "pve-postinstall.interfaces.mgmt_ip"))
        gateway = str(registry.get(host, "pve-postinstall.interfaces.gateway"))
        storage_ip = str(registry.get(host, "pve-postinstall.interfaces.storage_ip"))
    except HostLookupError as exc:
        raise ValueError(
            f"pve-postinstall.interfaces.{{mgmt_ip,gateway,storage_ip}} required for {host}"
        ) from exc

    render_file(
        root / "pve-postinstall" / "templates" / "pve-interfaces",
        build_dir / "interfaces",
        NET_MGMT_IP=mgmt_ip,
        NET_GATEWAY=gateway,
        NET_STORAGE_IP=storage_ip,
    )


def stage_and_install(
    root: Path,
    host: str,
    host_type: str,
    timezone: str,
    import_pools: str,
    mounts: str,
    build_dir: Path,
    connection: HostConnection,
    force: bool,
) -> None:
    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-postinstall" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        host_type,
        timezone,
        import_pools,
        mounts,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
