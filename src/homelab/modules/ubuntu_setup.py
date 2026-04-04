from __future__ import annotations

from pathlib import Path

from ..build import copy_file, copy_files, render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-ubuntu-setup"

STATIC_CONFIG_FILES = [
    "99-inotify.conf",
    "99-wireguard.conf",
    "sshd-hardening.conf",
    "zfs.conf",
    "zfs-scrub.timer",
]

ZFS_AUTOMATION_TEMPLATE_FILES = [
    "sanoid.conf",
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
    "homelab-zfs-replication.service",
    "homelab-zfs-replication.timer",
    "zfs_snapshots.sh",
    "zfs_replication_appdata.sh",
]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="ubuntu-setup")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping ubuntu-setup (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / "ubuntu-setup"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"
    scripts_dir = module_dir / "scripts"
    registry = default_registry(root)

    required_files = [
        scripts_dir / "install.sh",
        scripts_dir / "docker-install.sh",
        templates_dir / "rebuild.sh",
        templates_dir / "sudoers",
        templates_dir / "zfs-scrub.service",
        *[config_dir / file_name for file_name in STATIC_CONFIG_FILES],
    ]
    for file_path in required_files:
        if not file_path.is_file():
            raise ValueError(f"missing required file: {file_path}")

    for host in hosts:
        samba_enabled = str(registry.get(host, "ubuntu-setup.samba", "false")).lower()
        zfs_automation = registry.get(host, "ubuntu-setup.zfs_automation", {})
        if samba_enabled == "true":
            samba_config = config_dir / f"smb-{host}.conf"
            if not samba_config.is_file():
                raise ValueError(f"missing samba config: {samba_config}")
        if isinstance(zfs_automation, dict) and zfs_automation:
            for file_name in ZFS_AUTOMATION_TEMPLATE_FILES:
                file_path = templates_dir / file_name
                if not file_path.is_file():
                    raise ValueError(f"missing ZFS automation template: {file_path}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        host_type = registry.get(host, "config.type")
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type != "ubuntu":
        print_sub(f"Skipping {host}: ubuntu-setup supports type ubuntu only")
        return

    module_dir = root / "ubuntu-setup"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    user = str(registry.get(host, "config.user"))
    system_hostname = host
    system_timezone = str(registry.get(host, "ubuntu-setup.timezone", "UTC"))
    samba_enabled = str(registry.get(host, "ubuntu-setup.samba", "false")).lower()
    wireguard_enabled = str(registry.get(host, "ubuntu-setup.wireguard", "false")).lower()
    zfs_pool = str(registry.get(host, "ubuntu-setup.zfs_pool", "cache"))
    zfs_mountpoint = str(registry.get(host, "ubuntu-setup.zfs_mountpoint", f"/mnt/{zfs_pool}"))
    zfs_automation = registry.get(host, "ubuntu-setup.zfs_automation", {})
    zfs_automation_enabled = isinstance(zfs_automation, dict) and bool(zfs_automation)
    snapshot_schedule = str(
        registry.get(host, "ubuntu-setup.zfs_automation.snapshot_schedule", "*-*-* 04:35:00")
    )
    replication_schedule = str(
        registry.get(host, "ubuntu-setup.zfs_automation.replication_schedule", "*-*-* 02:30:00")
    )

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
    render_file(templates_dir / "sudoers", build_dir / "sudoers", USER=user)
    render_file(
        templates_dir / "zfs-scrub.service",
        build_dir / "zfs-scrub.service",
        ZFS_POOL=zfs_pool,
    )
    render_file(
        templates_dir / "rebuild.sh",
        build_dir / "rebuild.sh",
        SYSTEM_HOSTNAME=system_hostname,
        SYSTEM_TIMEZONE=system_timezone,
        ZFS_POOL=zfs_pool,
        ZFS_MOUNTPOINT=zfs_mountpoint,
    )
    if zfs_automation_enabled:
        render_file(templates_dir / "sanoid.conf", build_dir / "sanoid.conf", ZFS_POOL=zfs_pool)
        render_file(
            templates_dir / "homelab-zfs-snapshots.service",
            build_dir / "homelab-zfs-snapshots.service",
            ZFS_MOUNTPOINT=zfs_mountpoint,
        )
        render_file(
            templates_dir / "homelab-zfs-snapshots.timer",
            build_dir / "homelab-zfs-snapshots.timer",
            SNAPSHOT_SCHEDULE=snapshot_schedule,
        )
        render_file(
            templates_dir / "homelab-zfs-replication.service",
            build_dir / "homelab-zfs-replication.service",
            ZFS_MOUNTPOINT=zfs_mountpoint,
        )
        render_file(
            templates_dir / "homelab-zfs-replication.timer",
            build_dir / "homelab-zfs-replication.timer",
            REPLICATION_SCHEDULE=replication_schedule,
        )
        render_file(templates_dir / "zfs_snapshots.sh", build_dir / "zfs_snapshots.sh")
        render_file(
            templates_dir / "zfs_replication_appdata.sh",
            build_dir / "zfs_replication_appdata.sh",
            ZFS_POOL=zfs_pool,
        )

    if samba_enabled == "true":
        copy_file(config_dir / f"smb-{host}.conf", build_dir / "smb.conf")

    write_env_file(
        build_dir / "env",
        {
            "DEPLOY_USER": user,
            "SAMBA_ENABLED": samba_enabled,
            "SYSTEM_HOSTNAME": system_hostname,
            "SYSTEM_TIMEZONE": system_timezone,
            "WIREGUARD_ENABLED": wireguard_enabled,
            "ZFS_AUTOMATION_ENABLED": "true" if zfs_automation_enabled else "false",
            "ZFS_POOL": zfs_pool,
            "ZFS_MOUNTPOINT": zfs_mountpoint,
        },
    )

    connection = HostConnection(host)
    print_sub("Comparing with remote configs...")
    diffs = [
        (build_dir / "sudoers", f"/etc/sudoers.d/99-{user}-homelab"),
        (build_dir / "sshd-hardening.conf", "/etc/ssh/sshd_config.d/99-disable-password-auth.conf"),
        (build_dir / "zfs.conf", "/etc/modprobe.d/zfs.conf"),
        (build_dir / "99-inotify.conf", "/etc/sysctl.d/99-inotify.conf"),
        (build_dir / "zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service"),
        (build_dir / "zfs-scrub.timer", "/etc/systemd/system/zfs-scrub.timer"),
        (build_dir / "rebuild.sh", f"{zfs_mountpoint}/appdata/scripts/rebuild.sh"),
        (
            module_dir / "scripts" / "docker-install.sh",
            f"{zfs_mountpoint}/appdata/scripts/docker-install.sh",
        ),
    ]
    if wireguard_enabled == "true":
        diffs.append((build_dir / "99-wireguard.conf", "/etc/sysctl.d/99-wireguard.conf"))
    if zfs_automation_enabled:
        diffs.extend(
            [
                (build_dir / "sanoid.conf", "/etc/sanoid/sanoid.conf"),
                (
                    build_dir / "sanoid.conf",
                    f"{zfs_mountpoint}/appdata/scripts/sanoid.conf",
                ),
                (
                    build_dir / "zfs_snapshots.sh",
                    f"{zfs_mountpoint}/appdata/scripts/zfs_snapshots.sh",
                ),
                (
                    build_dir / "zfs_replication_appdata.sh",
                    f"{zfs_mountpoint}/appdata/scripts/zfs_replication_appdata.sh",
                ),
                (
                    build_dir / "homelab-zfs-snapshots.service",
                    "/etc/systemd/system/homelab-zfs-snapshots.service",
                ),
                (
                    build_dir / "homelab-zfs-snapshots.timer",
                    "/etc/systemd/system/homelab-zfs-snapshots.timer",
                ),
                (
                    build_dir / "homelab-zfs-replication.service",
                    "/etc/systemd/system/homelab-zfs-replication.service",
                ),
                (
                    build_dir / "homelab-zfs-replication.timer",
                    "/etc/systemd/system/homelab-zfs-replication.timer",
                ),
            ]
        )
    if samba_enabled == "true":
        diffs.append((build_dir / "smb.conf", "/etc/samba/smb.conf"))

    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy ubuntu-setup to {host}")
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
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
