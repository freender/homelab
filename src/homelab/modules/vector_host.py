from __future__ import annotations

import shutil
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, connection_for_host, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import build_files, diff_many
from ..templates import render_template
from .docker_stacks import declared_stacks
from .vector_config import JOURNALD_PARTIAL, WRAPPER_NAME

MODULE_DIR = "vector-host"
REMOTE_ROOT = "/tmp/homelab-vector-host"
VECTOR_CONFIG_DIR = "vector-config"
JOURNAL_UPLOAD = "journal-upload"
VICTORIA_LOGS_STACK = "victoria-logs"
VICTORIA_LOGS_PORT = 9428
METRICS_PORT = 9598
DATA_DIR = "/var/lib/vector"

CONFIG_NAME = "vector.yaml"
TESTS_NAME = "vector.test.yaml"
OVERRIDE_NAME = "vector-override.conf"
KEYRING_NAME = "datadog.asc"
SOURCE_NAME = "vector.list"

KEYRING_PATH = "/etc/apt/keyrings/datadog.asc"
WRAPPER_PATH = f"/usr/local/bin/{WRAPPER_NAME}"
# Not /etc/vector/vector.yaml: that is the package's conffile (homelab-ops#376).
CONFIG_PATH = "/etc/vector/homelab.yaml"
# `vector-0` holds back a 1.x release until this line is edited; nothing else is
# pinned, so apt-upgrade keeps Vector current like docker-ce.
APT_SOURCE = f"deb [signed-by={KEYRING_PATH}] https://apt.vector.dev/ stable vector-0\n"

# Install order is the installer's; this is every destination, for the diff and
# the file map alike.
FILE_SPECS = (
    FileSpec(KEYRING_NAME, KEYRING_PATH, "644"),
    FileSpec(SOURCE_NAME, "/etc/apt/sources.list.d/vector.list", "644"),
    FileSpec(WRAPPER_NAME, WRAPPER_PATH, "755"),
    FileSpec(OVERRIDE_NAME, "/etc/systemd/system/vector.service.d/homelab.conf", "644"),
    FileSpec(CONFIG_NAME, CONFIG_PATH, "644"),
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
        validate=lambda supported_hosts, _hosts: validate(root, supported_hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    required = [
        root / MODULE_DIR / "templates" / f"{name}.j2"
        for name in (CONFIG_NAME, TESTS_NAME, OVERRIDE_NAME)
    ]
    required += [
        root / MODULE_DIR / "configs" / KEYRING_NAME,
        root / MODULE_DIR / "scripts" / "install.py",
        root / VECTOR_CONFIG_DIR / "templates" / JOURNALD_PARTIAL,
        root / VECTOR_CONFIG_DIR / "configs" / WRAPPER_NAME,
    ]
    for path in required:
        if not path.is_file():
            raise ValueError(f"missing module file: {path}")
    victoria_logs_base(root)

    registry = default_registry(root)
    docker_vector = set(registry.list_hosts(VECTOR_CONFIG_DIR))
    uploading = set(registry.list_hosts(JOURNAL_UPLOAD))
    for host in sorted(hosts):
        if host in docker_vector:
            raise ValueError(
                f"{host}: runs the vector stack; ship its journal with "
                f"{VECTOR_CONFIG_DIR}.journald instead of {MODULE_DIR}"
            )
        # Two shippers on tenant 0 would double every journal line in Grafana,
        # log-spikes and the kernel rules.
        if not shadow_enabled(root, host) and host in uploading:
            raise ValueError(
                f"{host}: {MODULE_DIR} on tenant 0 needs {JOURNAL_UPLOAD} removed first; "
                f"set {MODULE_DIR}.shadow: true while both run"
            )


def shadow_enabled(root: Path, host: str) -> bool:
    """`shadow: true` ships to VictoriaLogs tenant 1, beside journal-upload."""
    value = default_registry(root).get(host, f"{MODULE_DIR}.shadow", False)
    if not isinstance(value, bool):
        raise ValueError(f"{host}: {MODULE_DIR}.shadow must be true or false, got {value!r}")
    return value


def victoria_logs_base(root: Path) -> str:
    """VictoriaLogs' base URL, from where the victoria-logs stack runs.

    Placement rather than a URL of its own, as journal-upload does: a second
    inventory key could only ever agree with docker-stacks or point every host at
    a VictoriaLogs that is not there.
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
    return f"http://{registry.get(hosts[0], 'config.hostname')}:{VICTORIA_LOGS_PORT}"


def render_build(root: Path, host: str) -> Path:
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    shadow = shadow_enabled(root, host)
    base = victoria_logs_base(root)
    templates = root / MODULE_DIR / "templates"
    partials = [root / VECTOR_CONFIG_DIR / "templates"]
    context = {
        "HOST": host,
        "SHADOW": shadow,
        "DATA_DIR": DATA_DIR,
        "JOURNALCTL_PATH": WRAPPER_PATH,
        "ENDPOINT": f"{base}/insert/elasticsearch/",
        "METRICS_PORT": METRICS_PORT,
    }
    for name in (CONFIG_NAME, TESTS_NAME):
        render_template(templates / f"{name}.j2", build_dir / name, partials, **context)
    render_template(
        templates / f"{OVERRIDE_NAME}.j2", build_dir / OVERRIDE_NAME, CONFIG=CONFIG_PATH
    )
    (build_dir / SOURCE_NAME).write_text(APT_SOURCE, encoding="utf-8")
    shutil.copyfile(root / MODULE_DIR / "configs" / KEYRING_NAME, build_dir / KEYRING_NAME)
    shutil.copy2(root / VECTOR_CONFIG_DIR / "configs" / WRAPPER_NAME, build_dir / WRAPPER_NAME)

    write_env_file(
        build_dir / "env",
        {
            "VECTOR_QUERY_URL": f"{base}/select/logsql/query",
            "VECTOR_TENANT": "1" if shadow else "0",
        },
    )
    write_file_map(build_dir, FILE_SPECS)
    return build_dir


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = render_build(root, host)

    connection = connection_for_host(root, host)
    diff_pairs = [(build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS]
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
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.py",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "scripts", "src"),
    )
