from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, HostArtifacts, require_text, write_file_map
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-snapraid-daemon"
TEMPLATE_FILES = ["snapraidd.conf"]


@dataclass(frozen=True)
class SnapRaidDaemonConfig:
    version: str
    deb_url: str
    sha256: str
    net_port: str
    net_acl: str
    probe_interval_minutes: int
    traefik_file_config_path: str | None
    traefik_route_host: str | None
    traefik_service_name: str | None
    traefik_service_url: str | None


FILE_SPECS = (
    FileSpec("snapraidd.conf", "/etc/snapraidd.conf"),
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="snapraid-daemon")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping snapraid-daemon (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / "snapraid-daemon"
    installer = module_dir / "scripts" / "install.sh"
    templates_dir = module_dir / "templates"

    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")
    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        if host not in registry.list_hosts(feature="snapraid"):
            raise ValueError(f"snapraid-daemon requires snapraid on {host}")
        if str(registry.get(host, "config.type")) == "pve":
            raise ValueError(f"snapraid-daemon must not run on PVE host {host}")
        normalize_config(registry, host)


def normalize_config(registry, host: str) -> SnapRaidDaemonConfig:
    version = require_text(
        registry.get(host, "snapraid-daemon.version", ""),
        f"snapraid-daemon.version is required for {host}",
    )
    deb_url = require_text(
        registry.get(host, "snapraid-daemon.deb_url", ""),
        f"snapraid-daemon.deb_url is required for {host}",
    )
    if not deb_url.startswith("https://"):
        raise ValueError(f"snapraid-daemon.deb_url must be https for {host}")

    sha256 = require_text(
        registry.get(host, "snapraid-daemon.sha256", ""),
        f"snapraid-daemon.sha256 is required for {host}",
    ).lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError(f"snapraid-daemon.sha256 must be a hex sha256 for {host}")

    net_port = require_text(
        registry.get(host, "snapraid-daemon.net_port", "127.0.0.1:7627"),
        f"snapraid-daemon.net_port is required for {host}",
    )

    net_acl_raw = registry.get(host, "snapraid-daemon.net_acl", ["+127.0.0.1"])
    if not isinstance(net_acl_raw, list) or not net_acl_raw:
        raise ValueError(f"snapraid-daemon.net_acl must be a non-empty list for {host}")
    net_acl = ",".join(
        require_text(entry, f"snapraid-daemon.net_acl entries must be non-empty for {host}")
        for entry in net_acl_raw
    )

    probe_interval_raw = registry.get(host, "snapraid-daemon.probe_interval_minutes", 3)
    try:
        probe_interval_minutes = int(probe_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"snapraid-daemon.probe_interval_minutes must be an integer for {host}"
        ) from exc
    if probe_interval_minutes < 0:
        raise ValueError(
            f"snapraid-daemon.probe_interval_minutes must be at least 0 for {host}"
        )

    traefik_file_config_path = optional_absolute_path(
        registry.get(host, "snapraid-daemon.traefik_file_config_path", ""),
        f"snapraid-daemon.traefik_file_config_path must be absolute for {host}",
    )
    traefik_route_host = optional_text(registry.get(host, "snapraid-daemon.traefik_route_host", ""))
    traefik_service_name = optional_text(
        registry.get(host, "snapraid-daemon.traefik_service_name", "")
    )
    traefik_service_url = optional_text(
        registry.get(host, "snapraid-daemon.traefik_service_url", "")
    )
    traefik_fields = [
        traefik_file_config_path,
        traefik_route_host,
        traefik_service_name,
        traefik_service_url,
    ]
    if any(traefik_fields) and not all(traefik_fields):
        raise ValueError(
            "snapraid-daemon Traefik fields must be set together "
            f"for {host}"
        )
    if traefik_service_url is not None and not traefik_service_url.startswith("http"):
        raise ValueError(f"snapraid-daemon.traefik_service_url must be http(s) for {host}")

    return SnapRaidDaemonConfig(
        version=version,
        deb_url=deb_url,
        sha256=sha256,
        net_port=net_port,
        net_acl=net_acl,
        probe_interval_minutes=probe_interval_minutes,
        traefik_file_config_path=traefik_file_config_path,
        traefik_route_host=traefik_route_host,
        traefik_service_name=traefik_service_name,
        traefik_service_url=traefik_service_url,
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    config = normalize_config(default_registry(root), host)
    module_dir = root / "snapraid-daemon"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        module_dir / "templates" / "snapraidd.conf",
        build_dir / "snapraidd.conf",
        NET_PORT=config.net_port,
        NET_ACL=config.net_acl,
        PROBE_INTERVAL_MINUTES=str(config.probe_interval_minutes),
    )
    write_env_file(
        build_dir / "snapraid-daemon.env",
        {
            "SNAPRAID_DAEMON_VERSION": config.version,
            "SNAPRAID_DAEMON_DEB_URL": config.deb_url,
            "SNAPRAID_DAEMON_SHA256": config.sha256,
            "TRAEFIK_FILE_CONFIG_PATH": config.traefik_file_config_path or "",
            "TRAEFIK_ROUTE_HOST": config.traefik_route_host or "",
            "TRAEFIK_SERVICE_NAME": config.traefik_service_name or "",
            "TRAEFIK_SERVICE_URL": config.traefik_service_url or "",
        },
    )
    write_file_map(build_dir, FILE_SPECS)
    return HostArtifacts(build_dir=build_dir, file_specs=FILE_SPECS)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    artifacts = build_host_artifacts(root, host)
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)

    print_sub("Comparing with remote configs...")
    diffs = [(artifacts.build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS]
    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "snapraid-daemon" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def optional_text(value: object) -> str | None:
    text = str(value).strip()
    return text or None


def optional_absolute_path(value: object, message: str) -> str | None:
    text = optional_text(value)
    if text is None:
        return None
    if not text.startswith("/"):
        raise ValueError(message)
    return text
