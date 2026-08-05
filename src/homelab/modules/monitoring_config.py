from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, normalize_bool
from ..output import print_action, print_sub
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
    for placeholder in ("__TELEGRAM_CHATID__", "__TELEGRAM_CHATID_PLEX__"):
        if alertmanager_template.count(placeholder) != 1:
            raise ValueError(f"alertmanager template must contain {placeholder} exactly once")


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
