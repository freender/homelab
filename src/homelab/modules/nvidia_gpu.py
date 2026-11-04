from __future__ import annotations

import shutil
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, connection_for_host, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import build_files, diff_many

FEATURE = "nvidia-gpu"
REMOTE_ROOT = "/tmp/homelab-nvidia-gpu"
INSTALLER = "scripts/install.py"
INTERPRETER = "python3"

# NVIDIA's CUDA repo signing key, vendored like docker.asc. Fingerprint
# 02182E60104FCDC26EAE1B8597A5D4CB8793F200, checked against the debian13
# InRelease it signs (freender/homelab-ops#300).
KEYRING_SPEC = FileSpec("nvidia-cuda.asc", "/etc/apt/keyrings/nvidia-cuda.asc", "644")
FILE_SPECS = (KEYRING_SPEC,)


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
        FEATURE,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def driver_branch(root: Path, host: str) -> str:
    """The NVIDIA driver branch the host is pinned to, e.g. `595`.

    Required, never defaulted: moving branches is a deliberate repo change, and a
    default would move every GPU host the day it changed.
    """
    value = str(default_registry(root).get(host, f"{FEATURE}.branch", "") or "").strip()
    if not value.isdigit():
        raise ValueError(f"{FEATURE}.branch must be a driver branch number (e.g. 595) for {host}")
    return value


def validate(root: Path, hosts: list[str]) -> None:
    key = root / FEATURE / "configs" / KEYRING_SPEC.build_name
    if not key.is_file():
        raise ValueError(f"missing required config: {key}")
    for host in hosts:
        driver_branch(root, host)


def render_build(root: Path, host: str, branch: str) -> Path:
    build_dir = root / FEATURE / "build" / host
    prepare_build_dir(build_dir)
    shutil.copyfile(
        root / FEATURE / "configs" / KEYRING_SPEC.build_name,
        build_dir / KEYRING_SPEC.build_name,
    )
    write_env_file(build_dir / "env", {"NVIDIA_BRANCH": branch})
    write_file_map(build_dir, FILE_SPECS)
    return build_dir


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    branch = driver_branch(root, host)
    build_dir = render_build(root, host, branch)

    connection = connection_for_host(root, host)
    print_sub("Comparing with remote files...")
    diff_pairs = [(build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS]
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would pin {host} to NVIDIA driver branch {branch}")
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
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
            (root / FEATURE / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        interpreter=INTERPRETER,
        remote_subdirs=("build", "src"),
    )
