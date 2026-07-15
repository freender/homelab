from __future__ import annotations

from pathlib import Path

from ..build import render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-wsl-conf"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="wsl-conf")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping wsl-conf (not applicable to {requested_host})")
        return 0

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def resolve_default_user(registry, host: str) -> str:
    # The interactive login user (config.ssh_config.user, e.g. "freender") is what
    # [user] default should be, not the root deploy user (config.user).
    return str(
        registry.get(host, "config.ssh_config.user", registry.get(host, "config.user"))
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        ssh_hostname = str(registry.get(host, "config.hostname", host))
        ssh_user = str(registry.get(host, "config.user"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    wsl_user = str(
        registry.get(host, "wsl-conf.default_user", resolve_default_user(registry, host))
    )
    wsl_hostname = str(registry.get(host, "wsl-conf.hostname", host))

    module_dir = root / "wsl-conf"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)
    render_file(
        module_dir / "templates" / "wsl.conf.tpl",
        build_dir / "wsl.conf",
        DEFAULT_USER=wsl_user,
        HOSTNAME=wsl_hostname,
    )

    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub("Comparing with remote config...")
    for message in diff_many(connection, [(build_dir / "wsl.conf", "/etc/wsl.conf")]):
        print_sub(message)

    if dry_run:
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
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
