from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, normalize_bool, run_module_deploy
from ..output import print_sub
from ..ssh import build_files, diff_many

MODULE_DIR = "monitoring-config"
REMOTE_ROOT = "/tmp/homelab-monitoring-config"
REMOTE_SCRAPE_CONFIG = "/mnt/cache/appdata/vmagent/scrape.yml"
REMOTE_ALERTMANAGER_CONFIG = "/mnt/cache/appdata/alertmanager/alertmanager.yml.tpl"
CONFIG_FILES = ("alertmanager.yml.tpl", "scrape.yml")


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
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    configs_dir = root / MODULE_DIR / "configs"
    actual_files = tuple(sorted(path.name for path in configs_dir.iterdir() if path.is_file()))
    if actual_files != CONFIG_FILES:
        raise ValueError(
            f"{MODULE_DIR} configs must be exactly {', '.join(CONFIG_FILES)}; "
            f"found {', '.join(actual_files) or 'none'}"
        )

    installer = root / MODULE_DIR / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")

    alertmanager_template = (configs_dir / "alertmanager.yml.tpl").read_text(encoding="utf-8")
    # The installer substitutes globally, so a chat ID may back more than one receiver
    # (the private chat serves both the default and the Proxmox receiver). Only the
    # absence of a placeholder is a real error.
    # __HEALTHCHECK_URL__ backs the dead-man's switch. It is a placeholder rather
    # than a literal for the same reason the chat IDs are: the ping URL is a
    # capability that would let anyone forge a healthy homelab, and this repo is
    # public. Requiring it here means the switch cannot be silently dropped from
    # the template.
    for placeholder in ("__TELEGRAM_CHATID__", "__TELEGRAM_CHATID_PLEX__", "__HEALTHCHECK_URL__"):
        if placeholder not in alertmanager_template:
            raise ValueError(f"alertmanager template must contain {placeholder}")


def alertmanager_enabled(root: Path, host: str) -> bool:
    registry = default_registry(root)
    return normalize_bool(
        registry.get(host, f"{MODULE_DIR}.alertmanager", None),
        False,
        f"{MODULE_DIR}.alertmanager must be true or false for {host}",
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    configs_dir = root / MODULE_DIR / "configs"
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    manages_alertmanager = alertmanager_enabled(root, host)
    write_env_file(
        build_dir / "env",
        {
            "ALERTMANAGER_ENABLED": "true" if manages_alertmanager else "false",
            "VMAGENT_CONTAINER": f"vmagent-{host}",
        },
    )

    connection = connection_for_host(root, host)
    diff_pairs = [(configs_dir / "scrape.yml", REMOTE_SCRAPE_CONFIG)]
    if manages_alertmanager:
        diff_pairs.append(
            (configs_dir / "alertmanager.yml.tpl", REMOTE_ALERTMANAGER_CONFIG)
        )
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy {MODULE_DIR} to {host}:{REMOTE_ROOT}/")
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
            (configs_dir, f"{REMOTE_ROOT}/configs"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="bash",
        remote_subdirs=("build", "configs", "scripts", "lib"),
    )
