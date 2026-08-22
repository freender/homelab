from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession, force_env, stage_and_run_remote_installer
from ..module_support import connection_for_host, run_module_deploy
from ..output import print_sub
from ..ssh import diff_many

MODULE_DIR = "vmalert-rules"
REMOTE_ROOT = "/tmp/homelab-vmalert-rules"
REMOTE_RULES_DIR = "/mnt/cache/appdata/vmalert/rules"
RULE_FILES = (
    "apt-updates.yml",
    "critical-containers.yml",
    "disk-labels.yml",
    "docker.yml",
    "important-containers.yml",
    "nic-link.yml",
    "node-down.yml",
    "reboot.yml",
    "sas-links.yml",
    "smart-disks.yml",
    "systemd-failed.yml",
    "temperatures.yml",
    "ups.yml",
    "watchdog.yml",
    "zfs-pools.yml",
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        MODULE_DIR,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    configs_dir = root / MODULE_DIR / "configs"
    actual_files = tuple(sorted(path.name for path in configs_dir.glob("*.yml")))
    if actual_files != RULE_FILES:
        raise ValueError(
            f"{MODULE_DIR} configs must be exactly {', '.join(RULE_FILES)}; "
            f"found {', '.join(actual_files) or 'none'}"
        )
    if not (root / MODULE_DIR / "scripts" / "install.sh").is_file():
        raise ValueError(f"missing installer: {root / MODULE_DIR / 'scripts' / 'install.sh'}")

    for host in hosts:
        if host != "helm":
            raise ValueError(f"{MODULE_DIR} supports helm only: {host}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    connection = connection_for_host(root, host)
    configs_dir = root / MODULE_DIR / "configs"
    for message in diff_many(
        connection,
        [(configs_dir / rule_file, f"{REMOTE_RULES_DIR}/{rule_file}") for rule_file in RULE_FILES],
    ):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy {MODULE_DIR} to {host}:{REMOTE_ROOT}/")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (configs_dir, f"{REMOTE_ROOT}/rules"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="bash",
        remote_subdirs=("rules", "scripts", "lib"),
    )
