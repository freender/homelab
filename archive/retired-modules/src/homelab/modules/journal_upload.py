from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, run_module_deploy
from ..output import print_sub
from ..ssh import build_files, diff_many
from ..templates import render_template
from .docker_stacks import declared_stacks

MODULE_DIR = "journal-upload"
REMOTE_ROOT = "/tmp/homelab-journal-upload"
REMOTE_CONFIG = "/etc/systemd/journal-upload.conf.d/homelab.conf"
REMOTE_OVERRIDE = "/etc/systemd/system/systemd-journal-upload.service.d/homelab.conf"
TEMPLATE_NAME = "journal-upload.conf.j2"
CONFIG_NAME = "journal-upload.conf"
OVERRIDE_TEMPLATE_NAME = "service-override.conf.j2"
OVERRIDE_NAME = "service-override.conf"
VICTORIA_LOGS_STACK = "victoria-logs"
VICTORIA_LOGS_PORT = 9428


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
    for name in (TEMPLATE_NAME, OVERRIDE_TEMPLATE_NAME):
        template = root / MODULE_DIR / "templates" / name
        if not template.is_file():
            raise ValueError(f"missing template: {template}")
    installer = root / MODULE_DIR / "scripts" / "install.py"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")
    upload_url(root)


def upload_url(root: Path) -> str:
    """The journald insert endpoint, derived from where the victoria-logs stack runs.

    Placement rather than a URL of its own: a second inventory key could only
    ever agree with docker-stacks or point every host at a VictoriaLogs that is
    not there. systemd-journal-upload appends `/upload` to this itself.
    """
    registry = default_registry(root)
    hosts = [
        host
        for host in registry.list_hosts("docker-stacks")
        if VICTORIA_LOGS_STACK in declared_stacks(root, host)
    ]
    if len(hosts) != 1:
        found = ", ".join(hosts) or "none"
        raise ValueError(
            f"{MODULE_DIR} needs exactly one host running the {VICTORIA_LOGS_STACK} stack, "
            f"found: {found}"
        )
    hostname = registry.get(hosts[0], "config.hostname")
    return f"http://{hostname}:{VICTORIA_LOGS_PORT}/insert/journald"


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    render_template(
        root / MODULE_DIR / "templates" / TEMPLATE_NAME,
        build_dir / CONFIG_NAME,
        URL=upload_url(root),
    )
    render_template(
        root / MODULE_DIR / "templates" / OVERRIDE_TEMPLATE_NAME,
        build_dir / OVERRIDE_NAME,
    )
    write_env_file(
        build_dir / "env",
        {"JOURNAL_UPLOAD_DEST": REMOTE_CONFIG, "JOURNAL_UPLOAD_OVERRIDE_DEST": REMOTE_OVERRIDE},
    )

    connection = connection_for_host(root, host)
    pairs = [(build_dir / CONFIG_NAME, REMOTE_CONFIG), (build_dir / OVERRIDE_NAME, REMOTE_OVERRIDE)]
    for message in diff_many(connection, pairs):
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
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.py",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "scripts", "src"),
    )
