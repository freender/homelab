from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fabric.transfer import Transfer
from invoke.exceptions import UnexpectedExit

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_error, print_sub, print_warn
from ..ssh import HostConnection, build_files, offline_diff, offline_mode

REMOTE_ROOT = "/tmp/homelab-ssh"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="ssh")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping ssh (not applicable to {requested_host})")
        return 0

    try:
        validate(root, supported_hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, supported_hosts: list[str]) -> None:
    configs_dir = root / "ssh" / "configs"
    common_config = configs_dir / "common.conf"
    if not common_config.is_file():
        raise ValueError(f"common config not found: {common_config}")

    for host in supported_hosts:
        host_dir = configs_dir / host
        append_config = host_dir / "append.conf"
        if host_dir.is_dir() and not append_config.is_file():
            print_warn(f"host config directory exists without append.conf: {host_dir}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    module_dir = root / "ssh"
    configs_dir = module_dir / "configs"
    build_dir = module_dir / "build" / host
    common_config = configs_dir / "common.conf"
    append_config = configs_dir / host / "append.conf"

    prepare_build_dir(build_dir)
    build_config(build_dir / "config", common_config, append_config)

    print_sub("Comparing with remote config...")
    if dry_run:
        status, message = dry_run_remote_diff(host, build_dir / "config")
    else:
        status, message = HostConnection(host).remote_diff(build_dir / "config", ".ssh/config")
    del status
    print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_install(root, host, build_dir, force=force)


def build_config(output_path: Path, common_config: Path, append_config: Path) -> None:
    content = common_config.read_text(encoding="utf-8")
    if append_config.is_file():
        content += append_config.read_text(encoding="utf-8")
    output_path.write_text(content, encoding="utf-8")


def dry_run_remote_diff(host: str, local_file: Path) -> tuple[int, str]:
    if offline_mode():
        return offline_diff("$HOME/.ssh/config")

    connection = HostConnection(host)
    transfer = Transfer(connection.connection)
    remote_path = ".ssh/config"
    temp_dir = Path(tempfile.mkdtemp(prefix="homelab-ssh-diff-"))
    temp_file = temp_dir / "config"

    try:
        try:
            transfer.get(remote_path, str(temp_file))
        except (FileNotFoundError, UnexpectedExit):
            return 2, f"[NEW] $HOME/{remote_path}"

        if local_file.read_bytes() == temp_file.read_bytes():
            return 0, f"[=] $HOME/{remote_path} (no changes)"

        return 1, f"[~] $HOME/{remote_path}"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def stage_and_install(root: Path, host: str, build_dir: Path, force: bool) -> None:
    connection = HostConnection(host)
    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "ssh" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=False,
        remote_subdirs=("build", "lib"),
    )
