from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import copy_file, copy_files, render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_ok, print_sub, print_warn
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-ubuntu-setup"
NETWORK_MACS_ENV = "network-macs.env"
TELEGRAM_ENV = "telegram.env"
NOTIFY_SCRIPT_DEST = "/usr/local/bin/homelab-notify-failure"

STATIC_CONFIG_FILES = [
    "99-inotify.conf",
    "sshd-hardening.conf",
    "zfs-scrub.timer",
]

ZFS_AUTOMATION_TEMPLATE_FILES = [
    "sanoid.conf",
    "homelab-zfs-snapshots.service",
    "homelab-zfs-snapshots.timer",
    "homelab-zfs-replication.service",
    "homelab-zfs-replication.timer",
]

NOTIFY_TEMPLATE_FILES = [
    "homelab-notify-failure@.service",
]



@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    zfs_mountpoint: str
    deploy_user: str
    samba_enabled: bool
    wireguard_enabled: bool
    zfs_automation_enabled: bool
    notifications_enabled: bool


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
    except ValueError:
        raise

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
        scripts_dir / "notify-failure.sh",
        scripts_dir / "pin-primary-nic.sh",
        templates_dir / "homelab-notify-failure@.service",
        templates_dir / "rebuild.sh",
        templates_dir / "10-network-names.rules",
        templates_dir / "sudoers",
        templates_dir / "zfs.conf",
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


def load_network_mac(root: Path, host: str) -> str:
    env_path = root / "secrets" / NETWORK_MACS_ENV
    if not env_path.is_file():
        return ""

    key = f"{host.upper().replace('-', '_')}_PRIMARY_INTERFACE_MAC"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        current_key, value = stripped.split("=", 1)
        if current_key.strip() != key:
            continue
        return value.strip().strip('"').strip("'")

    return ""


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    module_dir = root / "ubuntu-setup"
    try:
        host_type = registry.get(host, "config.type")
        ssh_hostname = str(registry.get(host, "config.hostname", host))
        ssh_user = str(registry.get(host, "config.user"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type != "ubuntu":
        print_sub(f"Skipping {host}: ubuntu-setup supports type ubuntu only")
        return

    artifacts = build_host_artifacts(root, host)

    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub("Comparing with remote configs...")
    diffs = [
        (artifacts.build_dir / "sudoers", f"/etc/sudoers.d/99-{artifacts.deploy_user}-homelab"),
        (artifacts.build_dir / "10-network-names.rules", "/etc/udev/rules.d/10-network-names.rules"),
        (artifacts.build_dir / "sshd-hardening.conf", "/etc/ssh/sshd_config.d/99-disable-password-auth.conf"),
        (artifacts.build_dir / "zfs.conf", "/etc/modprobe.d/zfs.conf"),
        (artifacts.build_dir / "99-inotify.conf", "/etc/sysctl.d/99-inotify.conf"),
        (artifacts.build_dir / "zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service"),
        (artifacts.build_dir / "zfs-scrub.timer", "/etc/systemd/system/zfs-scrub.timer"),
        (artifacts.build_dir / "rebuild.sh", f"{artifacts.zfs_mountpoint}/appdata/scripts/rebuild.sh"),
        (
            module_dir / "scripts" / "docker-install.sh",
            f"{artifacts.zfs_mountpoint}/appdata/scripts/docker-install.sh",
        ),
        (
            module_dir / "scripts" / "pin-primary-nic.sh",
            f"{artifacts.zfs_mountpoint}/appdata/scripts/pin-primary-nic.sh",
        ),
        (module_dir / "scripts" / "notify-failure.sh", NOTIFY_SCRIPT_DEST),
        (
            artifacts.build_dir / "homelab-notify-failure@.service",
            "/etc/systemd/system/homelab-notify-failure@.service",
        ),
    ]
    if artifacts.wireguard_enabled:
        diffs.append((artifacts.build_dir / "99-wireguard.conf", "/etc/sysctl.d/99-wireguard.conf"))
    if artifacts.zfs_automation_enabled:
        diffs.extend(
            [
                (artifacts.build_dir / "sanoid.conf", "/etc/sanoid/sanoid.conf"),
                (
                    artifacts.build_dir / "sanoid.conf",
                    f"{artifacts.zfs_mountpoint}/appdata/scripts/sanoid.conf",
                ),
                (
                    artifacts.build_dir / "homelab-zfs-snapshots.service",
                    "/etc/systemd/system/homelab-zfs-snapshots.service",
                ),
                (
                    artifacts.build_dir / "homelab-zfs-snapshots.timer",
                    "/etc/systemd/system/homelab-zfs-snapshots.timer",
                ),
                (
                    artifacts.build_dir / "homelab-zfs-replication.service",
                    "/etc/systemd/system/homelab-zfs-replication.service",
                ),
                (
                    artifacts.build_dir / "homelab-zfs-replication.timer",
                    "/etc/systemd/system/homelab-zfs-replication.timer",
                ),
            ]
        )
    if artifacts.samba_enabled:
        diffs.append((artifacts.build_dir / "smb.conf", "/etc/samba/smb.conf"))

    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy ubuntu-setup to {host}")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    module_dir = root / "ubuntu-setup"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    user = str(registry.get(host, "ubuntu-setup.deploy_user", registry.get(host, "config.user")))
    system_hostname = host
    system_timezone = str(registry.get(host, "ubuntu-setup.timezone", "UTC"))
    primary_interface_name = str(
        registry.get(host, "ubuntu-setup.network.pin_interface.name", "nic0")
    )
    primary_interface_mac = load_network_mac(root, host)
    samba_enabled = str(registry.get(host, "ubuntu-setup.samba", "false")).lower()
    wireguard_enabled = str(registry.get(host, "ubuntu-setup.wireguard", "false")).lower()
    zfs_pool = str(registry.get(host, "ubuntu-setup.zfs_pool", "cache"))
    zfs_mountpoint = str(registry.get(host, "ubuntu-setup.zfs_mountpoint", f"/mnt/{zfs_pool}"))
    zfs_arc_max = str(registry.get(host, "ubuntu-setup.zfs_arc_max", "8589934592"))
    zfs_automation = registry.get(host, "ubuntu-setup.zfs_automation", {})
    zfs_automation_enabled = isinstance(zfs_automation, dict) and bool(zfs_automation)
    snapshot_schedule = str(
        registry.get(host, "ubuntu-setup.zfs_automation.snapshot_schedule", "*-*-* 04:35:00")
    )
    replication_schedule = str(
        registry.get(host, "ubuntu-setup.zfs_automation.replication_schedule", "*-*-* 02:30:00")
    )
    replication_post_hook = str(
        registry.get(host, "ubuntu-setup.zfs_automation.replication_post_hook", "")
    )

    telegram_env_path = root / "secrets" / TELEGRAM_ENV
    notifications_enabled = telegram_env_path.is_file()

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
    render_file(templates_dir / "sudoers", build_dir / "sudoers", USER=user)
    render_file(templates_dir / "zfs.conf", build_dir / "zfs.conf", ZFS_ARC_MAX=zfs_arc_max)
    render_file(
        templates_dir / "zfs-scrub.service",
        build_dir / "zfs-scrub.service",
        ZFS_POOL=zfs_pool,
    )
    render_file(
        templates_dir / "10-network-names.rules",
        build_dir / "10-network-names.rules",
        INTERFACE_NAME=primary_interface_name,
        INTERFACE_MAC=primary_interface_mac,
    )
    render_file(
        templates_dir / "rebuild.sh",
        build_dir / "rebuild.sh",
        PRIMARY_INTERFACE_NAME=primary_interface_name,
        PRIMARY_INTERFACE_MAC=primary_interface_mac,
        SYSTEM_HOSTNAME=system_hostname,
        SYSTEM_TIMEZONE=system_timezone,
        ZFS_POOL=zfs_pool,
        ZFS_MOUNTPOINT=zfs_mountpoint,
    )
    if wireguard_enabled == "true":
        copy_file(config_dir / "99-wireguard.conf", build_dir / "99-wireguard.conf")
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
            ZFS_POOL=zfs_pool,
            REPLICATION_POST_HOOK=replication_post_hook,
        )
        render_file(
            templates_dir / "homelab-zfs-replication.timer",
            build_dir / "homelab-zfs-replication.timer",
            REPLICATION_SCHEDULE=replication_schedule,
        )

    if samba_enabled == "true":
        copy_file(config_dir / f"smb-{host}.conf", build_dir / "smb.conf")

    render_file(
        templates_dir / "homelab-notify-failure@.service",
        build_dir / "homelab-notify-failure@.service",
        NOTIFY_SCRIPT=NOTIFY_SCRIPT_DEST,
    )
    if notifications_enabled:
        copy_file(telegram_env_path, build_dir / "telegram.env")

    write_env_file(
        build_dir / "env",
        {
            "DEPLOY_USER": user,
            "PRIMARY_INTERFACE_NAME": primary_interface_name,
            "PRIMARY_INTERFACE_MAC": primary_interface_mac,
            "SAMBA_ENABLED": samba_enabled,
            "SYSTEM_HOSTNAME": system_hostname,
            "SYSTEM_TIMEZONE": system_timezone,
            "WIREGUARD_ENABLED": wireguard_enabled,
            "ZFS_AUTOMATION_ENABLED": "true" if zfs_automation_enabled else "false",
            "NOTIFICATIONS_ENABLED": "true" if notifications_enabled else "false",
            "ZFS_ARC_MAX": zfs_arc_max,
            "ZFS_POOL": zfs_pool,
            "ZFS_MOUNTPOINT": zfs_mountpoint,
        },
    )
    return HostArtifacts(
        build_dir=build_dir,
        zfs_mountpoint=zfs_mountpoint,
        deploy_user=user,
        samba_enabled=samba_enabled == "true",
        wireguard_enabled=wireguard_enabled == "true",
        zfs_automation_enabled=zfs_automation_enabled,
        notifications_enabled=notifications_enabled,
    )
