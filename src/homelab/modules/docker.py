from __future__ import annotations

import shutil
from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-docker"
INSTALLER = "scripts/install.py"
INTERPRETER = "python3"

HA_SWARM_DEFAULTS = {
    "tower": {
        "manager": "neo",
        "manager_addr": "neo.freender.internal:2377",
        "advertise_addr": "10.0.40.10",
    },
    "helm": {
        "manager": "tower",
        "manager_addr": "tower.freender.internal:2377",
        "advertise_addr": "10.0.40.245",
    },
    "neo": {
        "manager": "tower",
        "manager_addr": "tower.freender.internal:2377",
        "advertise_addr": "10.0.40.18",
    },
}

TEMPLATE_FILES = [
    "homelab-docker-update.service",
    "homelab-docker-update.timer",
]

SWARM_OVERLAY_NAME = "net_overlay"
SWARM_OVERLAY_SUBNET = "10.0.100.0/24"
SWARM_EXPECTED_NODES = "tower helm neo"

REMOTE_APPDATA = "/mnt/cache/appdata"
UPDATE_SERVICE = "homelab-docker-update.service"
UPDATE_TIMER = "homelab-docker-update.timer"


def swarm_env_values(host: str, swarm_defaults: dict) -> dict[str, str]:
    """The `DOCKER_SWARM_*` half of the env file.

    A host absent from `HA_SWARM_DEFAULTS` gets every value blank rather than the
    key omitted: the installer reads them unconditionally.
    """
    enabled = bool(swarm_defaults)
    return {
        "DOCKER_SWARM_ENABLED": "true" if enabled else "false",
        "DOCKER_SWARM_HOSTNAME": host,
        "DOCKER_SWARM_NODE_ROLE": "manager" if enabled else "",
        "DOCKER_SWARM_MANAGER": swarm_defaults.get("manager", ""),
        "DOCKER_SWARM_MANAGER_ADDR": swarm_defaults.get("manager_addr", ""),
        "DOCKER_SWARM_MANAGER_SSH": swarm_defaults.get("manager", ""),
        "DOCKER_SWARM_ADVERTISE_ADDR": swarm_defaults.get("advertise_addr", ""),
        "DOCKER_SWARM_OVERLAY_NAME": SWARM_OVERLAY_NAME if enabled else "",
        "DOCKER_SWARM_OVERLAY_SUBNET": SWARM_OVERLAY_SUBNET if enabled else "",
        "DOCKER_SWARM_EXPECTED_NODES": SWARM_EXPECTED_NODES if enabled else "",
    }


# Copied from `docker/scripts/` into the build dir, so every installed file has a
# `build/<host>/` source and one file map covers all of them.
HELPER_SCRIPTS = ("start.sh", "rm.sh", "rebuild.sh", "docker-common.sh")

# One owner for every destination: the dry-run diff and the file map the remote
# installer reads are both derived from these, so they cannot disagree.
HELPER_SPECS = (
    FileSpec("start.sh", f"{REMOTE_APPDATA}/start.sh", "755"),
    FileSpec("rm.sh", f"{REMOTE_APPDATA}/rm.sh", "755"),
    FileSpec("rebuild.sh", f"{REMOTE_APPDATA}/rebuild.sh", "755"),
    FileSpec("docker-common.sh", f"{REMOTE_APPDATA}/.homelab/docker/docker-common.sh", "755"),
    FileSpec("env", f"{REMOTE_APPDATA}/.homelab/docker/env", "644"),
)
UPDATE_UNIT_SPECS = (
    FileSpec(UPDATE_SERVICE, f"/etc/systemd/system/{UPDATE_SERVICE}", "644"),
    FileSpec(UPDATE_TIMER, f"/etc/systemd/system/{UPDATE_TIMER}", "644"),
)


def file_specs(update_timer: bool) -> tuple[FileSpec, ...]:
    """The units are managed only where the update timer is configured."""
    return HELPER_SPECS + (UPDATE_UNIT_SPECS if update_timer else ())


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
        "docker",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "docker" / "templates"
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")
    scripts_dir = root / "docker" / "scripts"
    for file_name in HELPER_SCRIPTS:
        file_path = scripts_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required script: {file_path}")


def render_build(root: Path, host: str, update_schedule: str) -> tuple[Path, tuple[FileSpec, ...]]:
    """Render everything the installer reads into `docker/build/<host>/`.

    An empty `update_schedule` means no update timer: its units are neither
    rendered nor put in the file map.
    """
    update_timer = bool(update_schedule)
    dependency_units: tuple[str, ...] = ()

    templates_dir = root / "docker" / "templates"
    build_dir = root / "docker" / "build" / host
    prepare_build_dir(build_dir)

    if update_timer:
        render_file(
            templates_dir / UPDATE_SERVICE,
            build_dir / UPDATE_SERVICE,
            DOCKER_DEPENDENCY_UNITS=" ".join(dependency_units),
        )
        render_file(
            templates_dir / UPDATE_TIMER,
            build_dir / UPDATE_TIMER,
            DOCKER_UPDATE_SCHEDULE=update_schedule,
        )

    write_env_file(
        build_dir / "env",
        {
            "ENABLE_DOCKER_UPDATE_TIMER": "true" if update_timer else "false",
            **swarm_env_values(host, HA_SWARM_DEFAULTS.get(host, {})),
        },
    )
    for name in HELPER_SCRIPTS:
        shutil.copyfile(root / "docker" / "scripts" / name, build_dir / name)
    specs = file_specs(update_timer)
    write_file_map(build_dir, specs)
    return build_dir, specs


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_user = str(registry.get(host, "config.user"))
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    update_schedule = str(registry.get(host, "docker.update_schedule", "")).strip()
    build_dir, specs = render_build(root, host, update_schedule)

    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub("Comparing with remote scripts...")
    diff_pairs = [(build_dir / spec.build_name, spec.remote_path) for spec in specs]
    for message in diff_many(connection, diff_pairs):
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
            (root / "docker" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        interpreter=INTERPRETER,
        remote_subdirs=("build", "lib"),
    )
