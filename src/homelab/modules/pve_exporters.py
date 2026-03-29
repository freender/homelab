from __future__ import annotations

from pathlib import Path

from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-exporters"
REQUIRED = [
    "node-exporter.defaults",
    "smartctl-exporter.defaults",
    "smartctl-exporter.service",
    "apcupsd-exporter.service",
    "apcupsd-exporter.py",
]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-exporters")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-exporters (not applicable to {requested_host})")
        return 0
    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    common_dir = root / "pve-exporters" / "configs" / "common"
    if not common_dir.is_dir():
        raise ValueError(f"configs/common not found: {common_dir}")
    for name in REQUIRED:
        if not (common_dir / name).is_file():
            raise ValueError(f"Missing required config: {common_dir / name}")
    if not apcupsd_exporter_env_template(root).is_file():
        raise ValueError(f"Missing required config: {apcupsd_exporter_env_template(root)}")


def has_apcupsd_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    role = str(registry.get(host, "apcupsd.role", "none"))
    return role in {"master", "master-standalone"}


def apcupsd_exporter_env_template(root: Path) -> Path:
    secrets_dir = root / "secrets"
    local_template = secrets_dir / "apcupsd-exporter.env"
    if local_template.is_file():
        return local_template
    return secrets_dir / "apcupsd-exporter.env.example"


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    common_dir = root / "pve-exporters" / "configs" / "common"
    build_dir = root / "pve-exporters" / "build" / host
    prepare_build_dir(build_dir)
    configs_dir = build_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    copy_files(common_dir, configs_dir, [
        "node-exporter.defaults",
        "smartctl-exporter.defaults",
        "smartctl-exporter.service",
    ])

    connection = HostConnection(host)
    if has_apcupsd_exporter(root, host):
        try:
            upsname = str(registry.get(host, "apcupsd.name"))
        except HostLookupError as exc:
            raise ValueError(str(exc)) from exc
        serial = ""
        if not dry_run:
            result = connection.connection.run(
                (
                    "apcaccess status 2>/dev/null | sed -n "
                    "'s/^SERIALNO[[:space:]]*:[[:space:]]*//p' | xargs"
                ),
                warn=True,
                hide=True,
            )
            serial = result.stdout.strip()
        copy_files(common_dir, configs_dir, ["apcupsd-exporter.py", "apcupsd-exporter.service"])
        render_file(
            apcupsd_exporter_env_template(root),
            configs_dir / "apcupsd-exporter.env",
            UPS_NAME=upsname,
            UPS_HOST=host,
            UPS_SERIAL=serial,
        )
        for message in diff_many(connection, [
            (configs_dir / "apcupsd-exporter.py", "/usr/local/bin/apcupsd-exporter"),
            (
                configs_dir / "apcupsd-exporter.service",
                "/etc/systemd/system/apcupsd-exporter.service",
            ),
            (configs_dir / "apcupsd-exporter.env", "/etc/default/apcupsd-exporter"),
        ]):
            print_sub(message)

    for message in diff_many(connection, [
        (configs_dir / "node-exporter.defaults", "/etc/default/prometheus-node-exporter"),
        (configs_dir / "smartctl-exporter.defaults", "/etc/default/smartctl-exporter"),
        (
            configs_dir / "smartctl-exporter.service",
            "/etc/systemd/system/smartctl-exporter.service",
        ),
    ]):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-exporters" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
