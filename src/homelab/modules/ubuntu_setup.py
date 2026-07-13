from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import op_secrets
from ..build import copy_file, copy_files, render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import copy_cached_secret, tmpfs_secret_stage
from ..modules.apcupsd import telegram_env_path
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-ubuntu-setup"
NETWORK_MACS_SECRET = "network-macs"
NOTIFY_SCRIPT = "notify-failure.sh"
NOTIFY_SERVICE = "homelab-notify-failure@.service"

STATIC_CONFIG_FILES = ["99-inotify.conf", "sshd-hardening.conf"]


@dataclass(frozen=True)
class FileSpec:
    build_name: str
    remote_path: str
    mode: str = "644"
    feature: str | None = None


@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    deploy_user: str
    samba_enabled: bool
    wireguard_enabled: bool
    notifications_enabled: bool
    file_specs: tuple[FileSpec, ...]


FILE_SPECS = (
    FileSpec("sudoers", "/etc/sudoers.d/99-{deploy_user}-homelab", mode="440"),
    FileSpec("10-network-names.rules", "/etc/udev/rules.d/10-network-names.rules"),
    FileSpec("sshd-hardening.conf", "/etc/ssh/sshd_config.d/01-disable-password-auth.conf"),
    FileSpec("zfs.conf", "/etc/modprobe.d/zfs.conf"),
    FileSpec("99-inotify.conf", "/etc/sysctl.d/99-inotify.conf"),
    FileSpec(
        NOTIFY_SCRIPT,
        "/usr/local/bin/homelab-notify-failure",
        mode="755",
        feature="notifications",
    ),
    FileSpec(
        NOTIFY_SERVICE,
        "/etc/systemd/system/homelab-notify-failure@.service",
        feature="notifications",
    ),
    FileSpec("telegram.env", "/etc/homelab/telegram.env", mode="600", feature="notifications"),
    FileSpec("99-wireguard.conf", "/etc/sysctl.d/99-wireguard.conf", feature="wireguard"),
    FileSpec("smb.conf", "/etc/samba/smb.conf", feature="samba"),
)


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

    validate(root, hosts)
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
        scripts_dir / "pin-primary-nic.sh",
        root / "shared" / "scripts" / NOTIFY_SCRIPT,
        root / "shared" / "templates" / NOTIFY_SERVICE,
        templates_dir / "10-network-names.rules",
        templates_dir / "sudoers",
        templates_dir / "zfs.conf",
        *[config_dir / file_name for file_name in STATIC_CONFIG_FILES],
    ]
    for file_path in required_files:
        if not file_path.is_file():
            raise ValueError(f"missing required file: {file_path}")

    for host in hosts:
        if str(registry.get(host, "ubuntu-setup.samba", "false")).lower() == "true":
            samba_config = config_dir / f"smb-{host}.conf"
            if not samba_config.is_file():
                raise ValueError(f"missing samba config: {samba_config}")

def load_network_mac(root: Path, host: str) -> str:
    try:
        env_path = op_secrets.secret_file(root, NETWORK_MACS_SECRET)
    except op_secrets.OpSecretsError:
        return ""
    if not env_path.is_file():
        return ""

    key = f"{host.upper().replace('-', '_')}_PRIMARY_INTERFACE_MAC"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        current_key, value = stripped.split("=", 1)
        if current_key.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def resolve_remote_path(spec: FileSpec, artifacts: HostArtifacts) -> str:
    return spec.remote_path.format(
        deploy_user=artifacts.deploy_user,
    )


def source_path_for_spec(root: Path, artifacts: HostArtifacts, spec: FileSpec) -> Path:
    if spec.build_name == "telegram.env":
        # Live bot token: read from the tmpfs secret cache, never rendered into build/.
        return telegram_env_path(root)
    return artifacts.build_dir / spec.build_name


def build_file_specs(artifacts: HostArtifacts) -> tuple[FileSpec, ...]:
    enabled_features = {
        "wireguard": artifacts.wireguard_enabled,
        "samba": artifacts.samba_enabled,
        "notifications": artifacts.notifications_enabled,
    }
    return tuple(
        spec
        for spec in FILE_SPECS
        if spec.feature is None or enabled_features.get(spec.feature, False)
    )


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
        (source_path_for_spec(root, artifacts, spec), resolve_remote_path(spec, artifacts))
        for spec in artifacts.file_specs
    ]
    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy ubuntu-setup to {host}")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        return

    upload_paths = [
        (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]

    with tmpfs_secret_stage("homelab-ubuntu-setup.") as secret_dir:
        if artifacts.notifications_enabled:
            upload_paths.append(
                (
                    copy_cached_secret(root, "telegram", secret_dir / "telegram.env"),
                    f"{REMOTE_ROOT}/build/{host}/telegram.env",
                )
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
            remote_subdirs=("build", "lib"),
        )


def write_file_map(build_dir: Path, artifacts: HostArtifacts) -> None:
    lines = [
        f"{spec.build_name}|{resolve_remote_path(spec, artifacts)}|{spec.mode}"
        for spec in artifacts.file_specs
    ]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    module_dir = root / "ubuntu-setup"
    config_dir = module_dir / "configs"
    templates_dir = module_dir / "templates"

    user = str(
        registry.get(host, "ubuntu-setup.deploy_user", registry.get(host, "config.user"))
    )
    system_hostname = host
    system_timezone = str(registry.get(host, "ubuntu-setup.timezone", "UTC"))
    primary_interface_name = str(
        registry.get(host, "ubuntu-setup.network.pin_interface.name", "nic0")
    )
    primary_interface_mac = load_network_mac(root, host)
    samba_enabled = str(registry.get(host, "ubuntu-setup.samba", "false")).lower() == "true"
    wireguard_enabled = str(registry.get(host, "ubuntu-setup.wireguard", "false")).lower() == "true"
    notifications_requested = (
        str(registry.get(host, "ubuntu-setup.notifications", "true")).lower() == "true"
    )
    zfs_arc_max = str(registry.get(host, "ubuntu-setup.zfs_arc_max", "8589934592"))
    notifications_enabled = False
    telegram_path: Path | None = None
    try:
        telegram_path = telegram_env_path(root)
        notifications_enabled = notifications_requested and telegram_path.is_file()
    except ValueError:
        notifications_enabled = False

    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    copy_files(config_dir, build_dir, STATIC_CONFIG_FILES)
    render_file(templates_dir / "sudoers", build_dir / "sudoers", USER=user)
    render_file(templates_dir / "zfs.conf", build_dir / "zfs.conf", ZFS_ARC_MAX=zfs_arc_max)
    render_file(
        templates_dir / "10-network-names.rules",
        build_dir / "10-network-names.rules",
        INTERFACE_NAME=primary_interface_name,
        INTERFACE_MAC=primary_interface_mac,
    )
    render_file(
        root / "shared" / "templates" / NOTIFY_SERVICE,
        build_dir / NOTIFY_SERVICE,
        NOTIFY_SCRIPT="/usr/local/bin/homelab-notify-failure",
    )
    copy_file(root / "shared" / "scripts" / NOTIFY_SCRIPT, build_dir / NOTIFY_SCRIPT)

    if wireguard_enabled:
        copy_file(config_dir / "99-wireguard.conf", build_dir / "99-wireguard.conf")
    if samba_enabled:
        copy_file(config_dir / f"smb-{host}.conf", build_dir / "smb.conf")
    # telegram.env deliberately NOT copied into build/: it holds a live bot token and
    # build/ is a persistent, mode-0644 directory in the repo. It is staged from the
    # tmpfs secret cache at upload time instead (see deploy_host).

    write_env_file(
        build_dir / "env",
        {
            "DEPLOY_USER": user,
            "PRIMARY_INTERFACE_NAME": primary_interface_name,
            "PRIMARY_INTERFACE_MAC": primary_interface_mac,
            "SAMBA_ENABLED": "true" if samba_enabled else "false",
            "SYSTEM_HOSTNAME": system_hostname,
            "SYSTEM_TIMEZONE": system_timezone,
            "WIREGUARD_ENABLED": "true" if wireguard_enabled else "false",
            "NOTIFICATIONS_ENABLED": "true" if notifications_enabled else "false",
            "ZFS_ARC_MAX": zfs_arc_max,
            "NOTIFY_SCRIPT_DEST": "/usr/local/bin/homelab-notify-failure",
            "TELEGRAM_ENV_DEST": "/etc/homelab/telegram.env",
        },
    )

    artifacts = HostArtifacts(
        build_dir=build_dir,
        deploy_user=user,
        samba_enabled=samba_enabled,
        wireguard_enabled=wireguard_enabled,
        notifications_enabled=notifications_enabled,
        file_specs=(),
    )
    file_specs = build_file_specs(artifacts)
    artifacts = HostArtifacts(
        build_dir=build_dir,
        deploy_user=user,
        samba_enabled=samba_enabled,
        wireguard_enabled=wireguard_enabled,
        notifications_enabled=notifications_enabled,
        file_specs=file_specs,
    )
    write_file_map(build_dir, artifacts)
    return artifacts
