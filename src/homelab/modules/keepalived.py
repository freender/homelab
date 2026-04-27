from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..build import render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, HostArtifacts, require_text, write_file_map
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many, offline_mode

REMOTE_ROOT = "/tmp/homelab-keepalived"
TEMPLATE_FILES = ["healthcheck.sh", "keepalived.conf"]
SECRETS_FILE = "keepalived.env"


@dataclass(frozen=True)
class KeepalivedConfig:
    instance_name: str
    interface: str
    healthcheck_host: str
    healthcheck_url: str
    virtual_router_id: int
    priority: int
    advert_interval: int
    unicast_src_ip: str
    unicast_peers: tuple[str, ...]
    virtual_ips: tuple[str, ...]


FILE_SPECS = (
    FileSpec("healthcheck.sh", "/etc/keepalived/healthcheck.sh", mode="755"),
    FileSpec("keepalived.conf", "/etc/keepalived/keepalived.conf"),
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="keepalived")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping keepalived (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    templates_dir = root / "keepalived" / "templates"
    installer = root / "keepalived" / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")

    for file_name in TEMPLATE_FILES:
        file_path = templates_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing required template: {file_path}")

    registry = default_registry(root)
    for host in hosts:
        normalize_config(root, registry, host)


def normalize_config(root: Path, registry, host: str) -> KeepalivedConfig:
    try:
        host_type = str(registry.get(host, "config.type"))
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type not in {"ubuntu", "pve"}:
        raise ValueError(f"keepalived supports PVE and Ubuntu hosts only: {host}")

    interface = require_text(
        registry.get(host, "keepalived.interface", ""),
        f"keepalived.interface is required for {host}",
    )
    instance_name = require_text(
        registry.get(host, "keepalived.instance_name", host),
        f"keepalived.instance_name must be non-empty for {host}",
    )
    healthcheck_values = load_keepalived_env(root)
    healthcheck_host_env = require_text(
        registry.get(host, "keepalived.healthcheck_host_env", ""),
        f"keepalived.healthcheck_host_env is required for {host}",
    )
    healthcheck_url_env = require_text(
        registry.get(host, "keepalived.healthcheck_url_env", ""),
        f"keepalived.healthcheck_url_env is required for {host}",
    )
    healthcheck_host = require_text(
        healthcheck_values.get(healthcheck_host_env, ""),
        f"{healthcheck_host_env} is required in {keepalived_env_path(root)} for {host}",
    )
    healthcheck_url = require_text(
        healthcheck_values.get(healthcheck_url_env, ""),
        f"{healthcheck_url_env} is required in {keepalived_env_path(root)} for {host}",
    )
    unicast_src_ip = require_text(
        registry.get(host, "keepalived.unicast_src_ip", ""),
        f"keepalived.unicast_src_ip is required for {host}",
    )

    virtual_router_id = int(registry.get(host, "keepalived.virtual_router_id", 0))
    priority = int(registry.get(host, "keepalived.priority", 0))
    advert_interval = int(registry.get(host, "keepalived.advert_interval", 1))
    if virtual_router_id < 1:
        raise ValueError(f"keepalived.virtual_router_id must be >= 1 for {host}")
    if priority < 1:
        raise ValueError(f"keepalived.priority must be >= 1 for {host}")
    if advert_interval < 1:
        raise ValueError(f"keepalived.advert_interval must be >= 1 for {host}")

    peers_raw = registry.get(host, "keepalived.unicast_peers", [])
    if not isinstance(peers_raw, list) or not peers_raw:
        raise ValueError(f"keepalived.unicast_peers must be a non-empty list for {host}")
    unicast_peers = tuple(
        require_text(peer, f"keepalived.unicast_peers entries must be non-empty for {host}")
        for peer in peers_raw
    )

    virtual_ips_raw = registry.get(host, "keepalived.virtual_ips", [])
    if not isinstance(virtual_ips_raw, list) or not virtual_ips_raw:
        raise ValueError(f"keepalived.virtual_ips must be a non-empty list for {host}")
    virtual_ips = tuple(
        require_text(item, f"keepalived.virtual_ips entries must be non-empty for {host}")
        for item in virtual_ips_raw
    )

    return KeepalivedConfig(
        instance_name=instance_name,
        interface=interface,
        healthcheck_host=healthcheck_host,
        healthcheck_url=healthcheck_url,
        virtual_router_id=virtual_router_id,
        priority=priority,
        advert_interval=advert_interval,
        unicast_src_ip=unicast_src_ip,
        unicast_peers=unicast_peers,
        virtual_ips=virtual_ips,
    )


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    config = normalize_config(root, registry, host)
    module_dir = root / "keepalived"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    render_file(
        module_dir / "templates" / "healthcheck.sh",
        build_dir / "healthcheck.sh",
        HEALTHCHECK_HOST=config.healthcheck_host,
        HEALTHCHECK_URL=config.healthcheck_url,
    )
    render_file(
        module_dir / "templates" / "keepalived.conf",
        build_dir / "keepalived.conf",
        INSTANCE_NAME=config.instance_name,
        INTERFACE=config.interface,
        VIRTUAL_ROUTER_ID=str(config.virtual_router_id),
        PRIORITY=str(config.priority),
        ADVERT_INTERVAL=str(config.advert_interval),
        UNICAST_SRC_IP=config.unicast_src_ip,
        UNICAST_PEERS="\n".join(f"        {peer}" for peer in config.unicast_peers),
        VIRTUAL_IPS="\n".join(f"        {vip}" for vip in config.virtual_ips),
    )

    write_file_map(build_dir, FILE_SPECS)
    return HostArtifacts(build_dir=build_dir, file_specs=FILE_SPECS)


def keepalived_env_path(root: Path) -> Path:
    secret = root / "secrets" / SECRETS_FILE
    if offline_mode() and not secret.is_file():
        return root / "secrets" / f"{SECRETS_FILE}.example"
    return secret


def load_keepalived_env(root: Path) -> dict[str, str]:
    path = keepalived_env_path(root)
    if not path.is_file():
        raise ValueError(
            f"missing keepalived env file: {path}; "
            f"copy secrets/{SECRETS_FILE}.example to secrets/{SECRETS_FILE}"
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"invalid env line in {path}:{line_number}")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_user = str(registry.get(host, "config.user"))
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    artifacts = build_host_artifacts(root, host)

    print_sub("Comparing with remote configs...")
    diff_pairs = [
        (artifacts.build_dir / spec.build_name, spec.remote_path)
        for spec in artifacts.file_specs
    ]
    for message in diff_many(connection, diff_pairs):
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
            (root / "keepalived" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=False,
        remote_subdirs=("build", "lib"),
    )
