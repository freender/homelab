from __future__ import annotations

from pathlib import Path

from .. import op_secrets
from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-pxe"
SECRET_NAME = "pve-pxe-token"

IPXE_MENUS = [
    "boot.ipxe",
    "pdm-auto-warning.ipxe",
    "pdm-auto.ipxe",
    "pve-load.ipxe",
    "pve-tui.ipxe",
    "pve-gui.ipxe",
    "pve-debug.ipxe",
    "pve-serial.ipxe",
    "autoexec.ipxe",
]

OPERATIONAL_SCRIPTS = [
    "pxe-enable",
    "pxe-disable",
    "pxe-autoupdate",
]

STATIC_UNITS = [
    "pxe-autoupdate.service",
]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-pxe")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-pxe (not applicable to {requested_host})")
        return 0

    try:
        validate(root)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    configs_dir = root / "pve-pxe" / "configs"
    templates_dir = root / "pve-pxe" / "templates"

    for name in IPXE_MENUS + OPERATIONAL_SCRIPTS + STATIC_UNITS:
        path = configs_dir / name
        if not path.is_file():
            raise ValueError(f"missing required config: {path}")

    for tmpl in ("pxe-mgmt.conf", "dnsmasq-pxe.conf", "nginx-pxe.conf", "pxe-autoupdate.timer"):
        path = templates_dir / tmpl
        if not path.is_file():
            raise ValueError(f"missing required template: {path}")

    # Validate secret resolves (offline: checks example file exists)
    try:
        op_secrets.secret_file(root, SECRET_NAME)
    except op_secrets.OpSecretsError as exc:
        raise ValueError(str(exc)) from exc


def _read_token(root: Path) -> str:
    """Read the PDM answer-auth token from the rendered 1Password secret."""
    env_path = op_secrets.secret_file(root, SECRET_NAME)
    env = op_secrets.parse_env_file(env_path)
    token = env.get("PVE_PXE_TOKEN", "").strip()
    if not token:
        raise ValueError(
            f"PVE_PXE_TOKEN is empty in rendered secret '{SECRET_NAME}'"
        )
    return token


def _read_pdm_cert_fingerprint(root: Path) -> str:
    env_path = op_secrets.secret_file(root, SECRET_NAME)
    env = op_secrets.parse_env_file(env_path)
    fingerprint = env.get("PDM_CERT_FINGERPRINT", "").strip()
    if not fingerprint:
        raise ValueError(
            f"PDM_CERT_FINGERPRINT is empty in rendered secret '{SECRET_NAME}'"
        )
    return fingerprint


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)

    mgmt_ip = str(registry.get(host, "pve-pxe.mgmt_ip"))
    pdm_url = str(registry.get(host, "pve-pxe.pdm_url"))
    pdm_cert_fingerprint = _read_pdm_cert_fingerprint(root)
    autoupdate_schedule = str(
        registry.get(host, "pve-pxe.autoupdate_schedule", "*-*-* 09:00:00")
    )

    # Derive the /24 network for dnsmasq dhcp-range from the management IP
    ip_parts = mgmt_ip.split(".")
    if len(ip_parts) != 4:
        raise ValueError(f"pve-pxe.mgmt_ip must be a dotted IPv4 address for {host}")
    mgmt_network = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0/24"

    configs_dir = root / "pve-pxe" / "configs"
    templates_dir = root / "pve-pxe" / "templates"
    build_dir = root / "pve-pxe" / "build" / host
    prepare_build_dir(build_dir)

    # Copy static iPXE menus, operational scripts, and service unit
    copy_files(configs_dir, build_dir, IPXE_MENUS + OPERATIONAL_SCRIPTS + STATIC_UNITS)

    # Render host-specific templates
    render_file(
        templates_dir / "pxe-mgmt.conf",
        build_dir / "pxe-mgmt.conf",
        MGMT_IP=mgmt_ip,
        PDM_URL=pdm_url,
        PDM_CERT_FINGERPRINT=pdm_cert_fingerprint,
    )
    render_file(
        templates_dir / "dnsmasq-pxe.conf",
        build_dir / "dnsmasq-pxe.conf",
        MGMT_IP=mgmt_ip,
        MGMT_NETWORK=mgmt_network,
    )
    render_file(
        templates_dir / "nginx-pxe.conf",
        build_dir / "nginx-pxe.conf",
        MGMT_IP=mgmt_ip,
    )
    render_file(
        templates_dir / "pxe-autoupdate.timer",
        build_dir / "pxe-autoupdate.timer",
        AUTOUPDATE_SCHEDULE=autoupdate_schedule,
    )

    # Resolve token and write to build dir (mode 600); never logged
    if not op_secrets.offline_mode():
        token = _read_token(root)
        token_file = build_dir / "homelab-pve-auto-install.token"
        token_file.write_text(token, encoding="utf-8")
        token_file.chmod(0o600)
        print_sub("Token resolved from 1Password")
    else:
        print_sub("[offline] token resolution skipped")

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )

    print_sub("Comparing with remote configs...")
    for message in diff_many(connection, [
        (build_dir / "pxe-mgmt.conf",             "/etc/saint/pxe-mgmt.conf"),
        (build_dir / "dnsmasq-pxe.conf",          "/etc/dnsmasq.d/pxe-mgmt.conf"),
        (build_dir / "nginx-pxe.conf",            "/etc/nginx/sites-available/pxe"),
        (build_dir / "pxe-autoupdate.service",    "/etc/systemd/system/pxe-autoupdate.service"),
        (build_dir / "pxe-autoupdate.timer",      "/etc/systemd/system/pxe-autoupdate.timer"),
        (build_dir / "boot.ipxe",                 "/srv/pxe/boot.ipxe"),
        (build_dir / "pve-load.ipxe",             "/srv/pxe/pve-load.ipxe"),
        (build_dir / "pxe-enable",                "/usr/local/sbin/pxe-enable"),
        (build_dir / "pxe-disable",               "/usr/local/sbin/pxe-disable"),
        (build_dir / "pxe-autoupdate",            "/usr/local/sbin/pxe-autoupdate"),
        (build_dir / "autoexec.ipxe",             "/srv/tftp/autoexec.ipxe"),
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
            (root / "pve-pxe" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
