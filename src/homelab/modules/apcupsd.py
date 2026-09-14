from __future__ import annotations

from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-apcupsd"
INSTALLER = "scripts/install.py"
INTERPRETER = "python3"

ROLES = ("master", "slave", "master-standalone")
# Roles that belong to the PVE HA cluster and so get the boot-time HA re-arm.
# `master-standalone` (osiris) is not a cluster member and must not have it.
HA_ROLES = frozenset({"master", "slave"})

# One owner for every destination: the dry-run diff and the file map the remote
# installer reads are both derived from this, so they cannot disagree about where
# a file goes.
APCUPSD_SPECS = (
    FileSpec("apcupsd.conf", "/etc/apcupsd/apcupsd.conf", "644"),
    FileSpec("doshutdown", "/etc/apcupsd/doshutdown", "755"),
)
HA_REARM_SPECS = (
    FileSpec("homelab-ha-rearm", "/usr/local/sbin/homelab-ha-rearm", "755"),
    FileSpec("homelab-ha-rearm.service", "/etc/systemd/system/homelab-ha-rearm.service", "644"),
)


def file_specs(role: str) -> tuple[FileSpec, ...]:
    return APCUPSD_SPECS + (HA_REARM_SPECS if role in HA_ROLES else ())


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    slave_hosts = get_slave_hosts(root)
    return run_module_deploy(
        root,
        requested_host,
        "apcupsd",
        session,
        lambda host: deploy_host(root, host, slave_hosts=slave_hosts, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    # Previously this only asserted the Telegram secret resolved, which is no
    # longer used here; the templates it actually depends on went unchecked.
    templates_dir = root / "apcupsd" / "templates"
    required = [
        "master.conf.tpl",
        "slave.conf.tpl",
        "doshutdown-master.tpl",
        "doshutdown-slave.tpl",
        "doshutdown-master-standalone.tpl",
        "homelab-ha-rearm.service.tpl",
        "homelab-ha-rearm.tpl",
        "_ha-functions.tpl",
    ]
    for name in required:
        if not (templates_dir / name).is_file():
            raise ValueError(f"missing apcupsd template: {templates_dir / name}")


def get_slave_hosts(root: Path) -> str:
    registry = default_registry(root)
    slaves: list[str] = []
    for host in registry.list_hosts(feature="apcupsd"):
        if registry.get(host, "apcupsd.role") == "slave":
            slaves.append(host)
    return " ".join(slaves)


def deploy_host(root: Path, host: str, slave_hosts: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        role = str(registry.get(host, "apcupsd.role"))
        upsname = str(registry.get(host, "apcupsd.name"))
        device = str(registry.get(host, "apcupsd.device", ""))
        nisip = str(registry.get(host, "apcupsd.nisip"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    build_dir = render_configs(root, host, role, upsname, device, nisip, slave_hosts)

    connection = HostConnection(host)
    print_sub("Comparing with remote configs...")
    files = [(build_dir / spec.build_name, spec.remote_path) for spec in file_specs(role)]
    for message in diff_many(connection, files):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_install(root, host, build_dir, connection, force=force)


def render_configs(
    root: Path,
    host: str,
    role: str,
    upsname: str,
    device: str,
    nisip: str,
    slave_hosts: str,
) -> Path:
    templates_dir = root / "apcupsd" / "templates"
    build_dir = root / "apcupsd" / "build" / host

    match role:
        case "master":
            conf_template = templates_dir / "master.conf.tpl"
            shutdown_template = templates_dir / "doshutdown-master.tpl"
        case "slave":
            conf_template = templates_dir / "slave.conf.tpl"
            shutdown_template = templates_dir / "doshutdown-slave.tpl"
        case "master-standalone":
            conf_template = templates_dir / "master.conf.tpl"
            shutdown_template = templates_dir / "doshutdown-master-standalone.tpl"
        case _:
            raise ValueError(f"Unknown role '{role}'")

    for template in [conf_template, shutdown_template]:
        if not template.is_file():
            raise ValueError(f"Missing template {template}")

    prepare_build_dir(build_dir)
    context = {
        "HOST": host,
        "UPSNAME": upsname,
        "DEVICE": device,
        "NISIP": nisip,
        "SLAVE_HOSTS": slave_hosts,
    }
    render_file(conf_template, build_dir / "apcupsd.conf", **context)
    render_file(shutdown_template, build_dir / "doshutdown", **context)
    if role in HA_ROLES:
        render_file(
            templates_dir / "homelab-ha-rearm.tpl",
            build_dir / "homelab-ha-rearm",
            **context,
        )
        render_file(
            templates_dir / "homelab-ha-rearm.service.tpl",
            build_dir / "homelab-ha-rearm.service",
            **context,
        )
        (build_dir / "homelab-ha-rearm").chmod(0o755)
    (build_dir / "doshutdown").chmod(0o755)
    write_env_file(build_dir / "env", {"ROLE": role, "HOST": host})
    write_file_map(build_dir, file_specs(role))
    return build_dir


def stage_and_install(
    root: Path,
    host: str,
    build_dir: Path,
    connection: HostConnection,
    force: bool,
) -> None:
    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "apcupsd" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
        interpreter=INTERPRETER,
    )
