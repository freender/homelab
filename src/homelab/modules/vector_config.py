from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, run_module_deploy
from ..output import print_sub
from ..ssh import build_files, diff_many
from ..templates import render_template
from .docker_stacks import appdata_root_for, declared_stacks

MODULE_DIR = "vector-config"
REMOTE_ROOT = "/tmp/homelab-vector-config"
CONFIG_NAME = "vector.yaml"
TESTS_NAME = "vector.test.yaml"
VECTOR_STACK = "vector"
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
    for template in (f"{CONFIG_NAME}.j2", f"{TESTS_NAME}.j2"):
        path = root / MODULE_DIR / "templates" / template
        if not path.is_file():
            raise ValueError(f"missing template: {path}")
    installer = root / MODULE_DIR / "scripts" / "install.py"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")

    # Checked in both directions across the whole inventory: a host running the
    # vector stack without this feature ships logs from a config in no repo, and
    # a host with the feature but no stack has no container to validate or reload.
    registry = default_registry(root)
    managed = set(registry.list_hosts(MODULE_DIR))
    running = {
        host
        for host in registry.list_hosts("docker-stacks")
        if VECTOR_STACK in declared_stacks(root, host)
    }
    if no_stack := sorted(managed - running):
        raise ValueError(
            f"{MODULE_DIR} enabled without the vector stack on: {', '.join(no_stack)}"
        )
    if unmanaged := sorted(running - managed):
        raise ValueError(
            f"vector stack runs without {MODULE_DIR} on: {', '.join(unmanaged)}"
        )
    for host in sorted(managed):
        shadow_enabled(root, host)


def geoip_enabled(root: Path, host: str) -> bool:
    """GeoIP enrichment follows the geoipupdate stack rather than a flag of its own:
    the database exists only where that stack writes it, so a second inventory key
    could only ever agree with the placement or point Vector at a missing file."""
    return GEOIP_STACK in declared_stacks(root, host)


def shadow_enabled(root: Path, host: str) -> bool:
    """`shadow: true` sends this host's logs to VictoriaLogs tenant 1, beside Alloy,
    until cutover (homelab-ops#367) removes the key."""
    value = default_registry(root).get(host, f"{MODULE_DIR}.shadow", False)
    if not isinstance(value, bool):
        raise ValueError(f"{host}: {MODULE_DIR}.shadow must be true or false, got {value!r}")
    return value


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    geoip = geoip_enabled(root, host)
    context = {"HOST": host, "GEOIP": geoip, "SHADOW": shadow_enabled(root, host)}
    for name in (CONFIG_NAME, TESTS_NAME):
        render_template(root / MODULE_DIR / "templates" / f"{name}.j2", build_dir / name, **context)

    appdata = appdata_root_for(root, host)
    remote_config = f"{appdata}/{VECTOR_STACK}/{CONFIG_NAME}"
    write_env_file(
        build_dir / "env",
        {
            "VECTOR_CONTAINER": f"vector-{host}",
            "VECTOR_DEST": remote_config,
            "VECTOR_GEOIP_DIR": f"{appdata}/{GEOIP_STACK}" if geoip else "",
        },
    )

    connection = connection_for_host(root, host)
    for message in diff_many(connection, [(build_dir / CONFIG_NAME, remote_config)]):
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
