from __future__ import annotations

import shutil
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
# Macros vector-host renders too, so the journal shape has one owner.
JOURNALD_PARTIAL = "journald.yaml.j2"
VECTOR_STACK = "vector"
GEOIP_STACK = "geoipupdate"
WRAPPER_NAME = "journalctl-catchup"
JOURNAL_UPLOAD = "journal-upload"


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
    required = [f"templates/{CONFIG_NAME}.j2", f"templates/{TESTS_NAME}.j2"]
    required += [f"templates/{JOURNALD_PARTIAL}"]
    required += ["scripts/install.py", f"configs/{WRAPPER_NAME}"]
    for name in required:
        path = root / MODULE_DIR / name
        if not path.is_file():
            raise ValueError(f"missing module file: {path}")

    registry = default_registry(root)
    managed = set(registry.list_hosts(MODULE_DIR))
    validate_stack_placement(root, managed)
    uploading = set(registry.list_hosts(JOURNAL_UPLOAD))
    for host in sorted(managed):
        shadow_enabled(root, host)
        # Two shippers on tenant 0 would double every journal line in Grafana,
        # log-spikes and the kernel rules.
        if journald_mode(root, host) == "on" and host in uploading:
            raise ValueError(
                f"{host}: {MODULE_DIR}.journald: true needs {JOURNAL_UPLOAD} removed first; "
                "use 'shadow' while both run"
            )


def validate_stack_placement(root: Path, managed: set[str]) -> None:
    """Checked in both directions across the whole inventory: a host running the
    vector stack without this feature ships logs from a config in no repo, and a
    host with the feature but no stack has no container to validate or reload."""
    running = {
        host
        for host in default_registry(root).list_hosts("docker-stacks")
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


def geoip_enabled(root: Path, host: str) -> bool:
    """GeoIP enrichment follows the geoipupdate stack rather than a flag of its own:
    the database exists only where that stack writes it, so a second inventory key
    could only ever agree with the placement or point Vector at a missing file."""
    return GEOIP_STACK in declared_stacks(root, host)


def shadow_enabled(root: Path, host: str) -> bool:
    """`shadow: true` sends this host's logs to VictoriaLogs tenant 1, so a new
    host can run beside another shipper before cutover (homelab-ops#366, #367)."""
    value = default_registry(root).get(host, f"{MODULE_DIR}.shadow", False)
    if not isinstance(value, bool):
        raise ValueError(f"{host}: {MODULE_DIR}.shadow must be true or false, got {value!r}")
    return value


def journald_mode(root: Path, host: str) -> str:
    """`journald` ships the host's systemd journal: `shadow` to tenant 1 beside
    journal-upload (homelab-ops#375), `true` to tenant 0 once that is retired."""
    value = default_registry(root).get(host, f"{MODULE_DIR}.journald", False)
    modes = {False: "", "shadow": "shadow", True: "on"}
    if isinstance(value, bool) or value == "shadow":
        return modes[value]
    raise ValueError(
        f"{host}: {MODULE_DIR}.journald must be true, false or 'shadow', got {value!r}"
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    geoip = geoip_enabled(root, host)
    journald = journald_mode(root, host)
    context = {
        "HOST": host,
        "GEOIP": geoip,
        "SHADOW": shadow_enabled(root, host),
        "JOURNALD": journald,
    }
    for name in (CONFIG_NAME, TESTS_NAME):
        render_template(root / MODULE_DIR / "templates" / f"{name}.j2", build_dir / name, **context)
    if journald:
        shutil.copy2(root / MODULE_DIR / "configs" / WRAPPER_NAME, build_dir / WRAPPER_NAME)

    appdata = appdata_root_for(root, host)
    remote_config = f"{appdata}/{VECTOR_STACK}/{CONFIG_NAME}"
    # A directory mount, unlike vector.yaml: Docker creates it empty on first
    # start, so the stack can be deployed before the wrapper exists.
    bin_dir = f"{appdata}/{VECTOR_STACK}/bin" if journald else ""
    write_env_file(
        build_dir / "env",
        {
            "VECTOR_CONTAINER": f"vector-{host}",
            "VECTOR_DEST": remote_config,
            "VECTOR_GEOIP_DIR": f"{appdata}/{GEOIP_STACK}" if geoip else "",
            "VECTOR_BIN_DIR": bin_dir,
        },
    )

    pairs = [(build_dir / CONFIG_NAME, remote_config)]
    if journald:
        pairs.append((build_dir / WRAPPER_NAME, f"{bin_dir}/{WRAPPER_NAME}"))
    connection = connection_for_host(root, host)
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
