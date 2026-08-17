from __future__ import annotations

from pathlib import Path

from ..build import copy_files
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, connection_for_host, feature_paused, write_file_map
from ..output import print_action, print_sub
from ..ssh import diff_many

MODULE_DIR = "apt-security-updates"
REMOTE_ROOT = "/tmp/homelab-apt-security-updates"

# Both files are static: the origin pattern uses APT's own ${distro_codename}
# expansion rather than a value rendered here, so it keeps working across a
# Debian major upgrade without this module needing to know the codename.
FILE_SPECS = (
    FileSpec(
        "52homelab-security-updates",
        "/etc/apt/apt.conf.d/52homelab-security-updates",
    ),
    FileSpec(
        "20homelab-auto-upgrades",
        "/etc/apt/apt.conf.d/20homelab-auto-upgrades",
    ),
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

    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    configs_dir = root / MODULE_DIR / "configs"
    if not configs_dir.is_dir():
        raise ValueError(f"configs not found: {configs_dir}")
    for spec in FILE_SPECS:
        if not (configs_dir / spec.build_name).is_file():
            raise ValueError(f"Missing required config: {configs_dir / spec.build_name}")
    if not (root / MODULE_DIR / "scripts" / "install.sh").is_file():
        raise ValueError(f"missing installer: {root / MODULE_DIR / 'scripts' / 'install.sh'}")

    conflicts = conflicting_hosts(root)
    if conflicts:
        raise ValueError(
            "hosts enable both apt-security-updates and apt-upgrade, which would "
            "dist-upgrade Proxmox packages unattended and defeat the security-only "
            f"scope: {', '.join(conflicts)}"
        )


def conflicting_hosts(root: Path) -> list[str]:
    """Hosts enabling both this module and apt-upgrade.

    apt-upgrade installs a timer that runs a full `apt-get -y dist-upgrade`.
    Enabling it alongside this module would leave the narrow origin scope in
    place while something else ignored it entirely, so the combination is
    rejected in validate() rather than discovered on the host. install.sh
    repeats the check against live systemd state, since a host can carry a
    leftover timer from a previous deploy that hosts.conf no longer describes.
    """
    registry = default_registry(root)
    secure = set(registry.list_hosts(feature=MODULE_DIR))
    dist_upgrade = set(registry.list_hosts(feature="apt-upgrade"))
    return sorted(secure & dist_upgrade)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    configs_dir = root / MODULE_DIR / "configs"
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)
    copy_files(configs_dir, build_dir, [spec.build_name for spec in FILE_SPECS])
    write_file_map(build_dir, FILE_SPECS)

    paused = feature_paused(registry, host, MODULE_DIR)
    connection = connection_for_host(root, host)

    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [(build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS],
    ):
        print_sub(message)

    if dry_run:
        if paused:
            print_sub(
                f"[DRY-RUN] Would pause {MODULE_DIR} on {host} "
                "(disable apt-daily-upgrade.timer, leave config in place)"
            )
        else:
            print_sub(
                f"[DRY-RUN] Would enable Debian-security-only unattended upgrades on {host}"
            )
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        return

    env = force_env(force)
    env["PAUSED"] = "true" if paused else "false"
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
        env=env,
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
