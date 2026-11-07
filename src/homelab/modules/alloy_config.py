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

MODULE_DIR = "alloy-config"
REMOTE_ROOT = "/tmp/homelab-alloy-config"
REMOTE_CONFIG = "/mnt/cache/appdata/alloy/config.alloy"
TEMPLATE_NAME = "config.alloy.j2"
CONFIG_NAME = "config.alloy"
ALLOY_STACK = "alloy"
GEOIP_STACK = "geoipupdate"


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
    template = root / MODULE_DIR / "templates" / TEMPLATE_NAME
    if not template.is_file():
        raise ValueError(f"missing template: {template}")
    installer = root / MODULE_DIR / "scripts" / "install.py"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")

    # Checked in both directions across the whole inventory: a host running the
    # alloy stack without this feature ships logs from a config in no repo, and
    # a host with the feature but no stack has no container to validate or reload.
    registry = default_registry(root)
    managed = set(registry.list_hosts(MODULE_DIR))
    running = {
        host
        for host in registry.list_hosts("docker-stacks")
        if ALLOY_STACK in declared_stacks(root, host)
    }
    if no_stack := sorted(managed - running):
        raise ValueError(
            f"{MODULE_DIR} enabled without the alloy stack on: {', '.join(no_stack)}"
        )
    if unmanaged := sorted(running - managed):
        raise ValueError(
            f"alloy stack runs without {MODULE_DIR} on: {', '.join(unmanaged)}"
        )


def geoip_enabled(root: Path, host: str) -> bool:
    """GeoIP enrichment follows the geoipupdate stack rather than a flag of its own:
    the database exists only where that stack writes it, so a second inventory key
    could only ever agree with the placement or point Alloy at a missing file."""
    return GEOIP_STACK in declared_stacks(root, host)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    render_template(
        root / MODULE_DIR / "templates" / TEMPLATE_NAME,
        build_dir / CONFIG_NAME,
        HOST=host,
        GEOIP=geoip_enabled(root, host),
    )
    write_env_file(
        build_dir / "env",
        {
            "ALLOY_CONTAINER": f"alloy-{host}",
            "ALLOY_DEST": REMOTE_CONFIG,
        },
    )

    connection = connection_for_host(root, host)
    for message in diff_many(connection, [(build_dir / CONFIG_NAME, REMOTE_CONFIG)]):
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
