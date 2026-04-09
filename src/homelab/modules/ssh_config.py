from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fabric.transfer import Transfer
from invoke.exceptions import UnexpectedExit

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, offline_diff, offline_mode

REMOTE_ROOT = "/tmp/homelab-ssh-config"
IDENTITY_FILES = {
    "homelab": "id_ed25519",
    "infra": "id_ed25519_pve",
}


def collect_ssh_entries(registry) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for host in registry.list_hosts():
        try:
            hostname = str(registry.get(host, "config.hostname"))
            default_user = str(registry.get(host, "config.user"))
            default_sshkey = str(registry.get(host, "config.sshkey"))
        except (HostLookupError, ValueError):
            continue

        user = str(registry.get(host, "config.ssh_config.user", default_user))
        sshkey = str(registry.get(host, "config.ssh_config.sshkey", default_sshkey))
        entries.append(
            {
                "name": host,
                "hostname": hostname,
                "user": user,
                "sshkey": sshkey,
            }
        )

        if user != default_user or sshkey != default_sshkey:
            entries.append(
                {
                    "name": f"{host}-root",
                    "hostname": hostname,
                    "user": default_user,
                    "sshkey": default_sshkey,
                }
            )

    return entries


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="ssh-config")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping ssh-config (not applicable to {requested_host})")
        return 0

    try:
        validate(root, supported_hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, supported_hosts: list[str]) -> None:
    del supported_hosts
    configs_dir = root / "ssh-config" / "configs"
    common_config = configs_dir / "common.conf"
    if not common_config.is_file():
        raise ValueError(f"common config not found: {common_config}")

    registry = default_registry(root)
    for entry in collect_ssh_entries(registry):
        if entry["sshkey"] not in IDENTITY_FILES:
            raise ValueError(f"unknown ssh key '{entry['sshkey']}' for generated config")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    module_dir = root / "ssh-config"
    configs_dir = module_dir / "configs"
    build_dir = module_dir / "build" / host
    common_config = configs_dir / "common.conf"

    prepare_build_dir(build_dir)
    build_config(root, host, build_dir / "config", common_config)

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


def build_config(root: Path, deploy_host: str, output_path: Path, common_config: Path) -> None:
    registry = default_registry(root)
    content = common_config.read_text(encoding="utf-8").rstrip() + "\n\n"
    ssh_entries = collect_ssh_entries(registry)
    host_config = render_host_config(ssh_entries)
    if host_config:
        content += host_config + "\n\n"
    identities_config = render_identities_config(registry, deploy_host, ssh_entries)
    if identities_config:
        content += identities_config + "\n"
    output_path.write_text(content, encoding="utf-8")


def render_host_config(ssh_entries: list[dict[str, str]]) -> str:
    blocks: list[str] = []
    for entry in ssh_entries:
        blocks.append(
            "\n".join(
                [
                    f"Host {entry['name']}",
                    f"  HostName {entry['hostname']}",
                    f"  User {entry['user']}",
                ]
            )
        )

    return "\n\n".join(blocks)


def render_identities_config(registry, deploy_host: str, ssh_entries: list[dict[str, str]]) -> str:
    agent = registry.get(deploy_host, "config.agent", "ssh-add")
    suffix = ".pub" if agent == "op" else ""
    grouped_hosts: dict[str, list[str]] = {}

    for entry in ssh_entries:
        grouped_hosts.setdefault(entry["sshkey"], []).append(entry["name"])

    blocks: list[str] = []
    for key_name, hosts in grouped_hosts.items():
        identity_file = IDENTITY_FILES.get(key_name)
        if identity_file is None:
            raise ValueError(f"unknown ssh key '{key_name}' for generated config")
        blocks.append(
            "\n".join(
                [
                    f"Host {' '.join(hosts)}",
                    f"  IdentityFile ~/.ssh/{identity_file}{suffix}",
                    "  IdentitiesOnly yes",
                ]
            )
        )

    return "\n\n".join(blocks)


def dry_run_remote_diff(host: str, local_file: Path) -> tuple[int, str]:
    if offline_mode():
        return offline_diff("$HOME/.ssh/config")

    connection = HostConnection(host)
    transfer = Transfer(connection.connection)
    remote_path = ".ssh/config"
    temp_dir = Path(tempfile.mkdtemp(prefix="homelab-ssh-config-diff-"))
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
            (root / "ssh-config" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=False,
        remote_subdirs=("build", "lib"),
    )
