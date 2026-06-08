from __future__ import annotations

import ctypes
import random
import string
from pathlib import Path
from typing import Any

from .. import op_secrets
from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, diff_many, offline_mode
from .pve_autoinstall import (
    _get_mgmt_mac,
    _is_pdm_host,
    _read_secret_field,
    _root_password_secret,
)

REMOTE_ROOT = "/tmp/homelab-pve-pxe"
SECRET_NAME = "pve-pxe-token"
PXE_CONFIG_DIR = "/etc/homelab-pxe"
ISO_ANSWER_DIR = f"{PXE_CONFIG_DIR}/iso-answers"

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
    "iso-autobuild",
]

STATIC_UNITS = [
    "pxe-autoupdate.service",
    "iso-autobuild.service",
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
    registry = default_registry(root)
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

    iso_hosts = _iso_target_hosts(registry)
    if not iso_hosts:
        return

    pdm_host = _find_pdm_host(registry)
    if pdm_host is None:
        raise ValueError("pve-pxe: no PDM host found; cannot load baked ISO globals")
    _get_iso_global_cfg(registry, pdm_host)

    for host in iso_hosts:
        try:
            registry.get(host, "pve-autoinstall.boot_disk_serial")
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.boot_disk_serial missing for ISO host {host}")
        _get_mgmt_mac(registry, host)

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

    # Derive the /24 network for dnsmasq proxyDHCP from the management IP.
    ip_parts = mgmt_ip.split(".")
    if len(ip_parts) != 4:
        raise ValueError(f"pve-pxe.mgmt_ip must be a dotted IPv4 address for {host}")
    mgmt_network = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0/24"
    mgmt_proxy_network = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0"

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
        MGMT_PROXY_NETWORK=mgmt_proxy_network,
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

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
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

    rendered_iso_hosts = _render_iso_answers(
        root,
        registry,
        build_dir,
        connection,
        force=force,
        dry_run=dry_run,
    )

    print_sub("Comparing with remote configs...")
    for message in diff_many(connection, [
        (build_dir / "pxe-mgmt.conf",             f"{PXE_CONFIG_DIR}/pxe-mgmt.conf"),
        (build_dir / "dnsmasq-pxe.conf",          "/etc/dnsmasq.d/pxe-mgmt.conf"),
        (build_dir / "nginx-pxe.conf",            "/etc/nginx/sites-available/pxe"),
        (build_dir / "pxe-autoupdate.service",    "/etc/systemd/system/pxe-autoupdate.service"),
        (build_dir / "pxe-autoupdate.timer",      "/etc/systemd/system/pxe-autoupdate.timer"),
        (build_dir / "boot.ipxe",                 "/srv/pxe/boot.ipxe"),
        (build_dir / "pve-load.ipxe",             "/srv/pxe/pve-load.ipxe"),
        (build_dir / "pxe-enable",                "/usr/local/sbin/pxe-enable"),
        (build_dir / "pxe-disable",               "/usr/local/sbin/pxe-disable"),
        (build_dir / "pxe-autoupdate",            "/usr/local/sbin/pxe-autoupdate"),
        (build_dir / "iso-autobuild",             "/usr/local/sbin/iso-autobuild"),
        (build_dir / "iso-autobuild.service",     "/etc/systemd/system/iso-autobuild.service"),
        (build_dir / "autoexec.ipxe",             "/srv/tftp/autoexec.ipxe"),
    ]):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        if rendered_iso_hosts:
            print_sub(f"[DRY-RUN] Would stage baked ISO answers: {' '.join(rendered_iso_hosts)}")
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


def _iso_target_hosts(registry: Any) -> list[str]:
    return [
        h for h in registry.list_hosts(feature="pve-autoinstall")
        if not _is_pdm_host(registry, h) and _wants_iso(registry, h)
    ]


def _wants_iso(registry: Any, host: str) -> bool:
    try:
        return bool(registry.get(host, "pve-autoinstall.iso_build"))
    except HostLookupError:
        return False


def _find_pdm_host(registry: Any) -> str | None:
    pdm_hosts = [
        h for h in registry.list_hosts(feature="pve-autoinstall")
        if _is_pdm_host(registry, h)
    ]
    return pdm_hosts[0] if pdm_hosts else None


def _get_iso_global_cfg(registry: Any, pdm_host: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    for key in ("mailto", "keyboard", "country"):
        try:
            cfg[key] = str(registry.get(pdm_host, f"pve-autoinstall.{key}"))
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.{key} missing for PDM host {pdm_host}")
    return cfg


def _render_iso_answers(
    root: Path,
    registry: Any,
    build_dir: Path,
    connection: HostConnection,
    *,
    force: bool,
    dry_run: bool,
) -> list[str]:
    iso_hosts = _iso_target_hosts(registry)
    if not iso_hosts:
        return []

    pdm_host = _find_pdm_host(registry)
    if pdm_host is None:
        raise ValueError("pve-pxe: no PDM host found; cannot render baked ISO answers")
    global_cfg = _get_iso_global_cfg(registry, pdm_host)

    if dry_run:
        print_sub(f"Baked ISO targets: {' '.join(iso_hosts)}")
        return iso_hosts
    if offline_mode():
        print_sub("[offline] baked ISO answer rendering skipped")
        return []

    if force:
        to_render = iso_hosts
        print_sub("--force: re-rendering all baked ISO answers from 1Password")
    else:
        to_render = _check_missing_answer_files(connection, iso_hosts)
        if to_render:
            print_sub(f"Missing baked ISO answers: {' '.join(to_render)}")
        else:
            print_sub("Baked ISO answers already present on PXE host; use --force to refresh")

    if not to_render:
        return []

    answer_dir = build_dir / "iso-answers"
    answer_dir.mkdir(parents=True, exist_ok=True)

    print_sub("Rendering baked ISO answers...")
    for iso_host in to_render:
        secret_name = _root_password_secret(registry, iso_host)
        plaintext = _read_secret_field(root, secret_name, "PVE_ROOT_PASSWORD")
        pw_hash = _hash_password(plaintext)
        toml = _build_iso_answer_toml(root, registry, iso_host, global_cfg, pw_hash)
        answer_file = answer_dir / f"{iso_host}.toml"
        answer_file.write_text(toml, encoding="utf-8")
        answer_file.chmod(0o600)
        print_sub(f"  rendered {iso_host}.toml")

    return to_render


def _check_missing_answer_files(connection: HostConnection, hosts: list[str]) -> list[str]:
    try:
        result = connection.connection.run(
            f"ls {ISO_ANSWER_DIR}/ 2>/dev/null || true",
            hide=True,
        )
        existing = set(result.stdout.strip().split()) if result.stdout.strip() else set()
    except Exception:
        existing = set()
    return [host for host in hosts if f"{host}.toml" not in existing]


def _get_timezone(registry: Any, host: str) -> str:
    for key in (
        "pve-autoinstall.timezone",
        "pve-postinstall.timezone",
        "ubuntu-setup.timezone",
    ):
        try:
            return str(registry.get(host, key))
        except HostLookupError:
            continue
    return "UTC"


def _build_iso_answer_toml(
    root: Path,
    registry: Any,
    host: str,
    global_cfg: dict[str, str],
    root_password_hash: str,
) -> str:
    boot_disk_serial = str(registry.get(host, "pve-autoinstall.boot_disk_serial"))

    try:
        cidr = str(registry.get(host, "pve-autoinstall.cidr"))
    except HostLookupError:
        cidr = str(registry.get(host, "pve-postinstall.interfaces.mgmt_ip"))

    try:
        gateway = str(registry.get(host, "pve-autoinstall.gateway"))
    except HostLookupError:
        gateway = str(registry.get(host, "pve-postinstall.interfaces.gateway"))

    try:
        dns = str(registry.get(host, "pve-autoinstall.dns"))
    except HostLookupError:
        dns = gateway

    fqdn = str(registry.get(host, "config.hostname"))
    timezone = _get_timezone(registry, host)
    mgmt_mac = _get_mgmt_mac(registry, host)

    return (
        f"# Auto-generated by homelab pve-pxe. Do not edit manually.\n"
        f"# Redeploy with: ./deploy --force pve-pxe arc\n"
        f"\n"
        f"[global]\n"
        f'keyboard = "{global_cfg["keyboard"]}"\n'
        f'country = "{global_cfg["country"]}"\n'
        f'fqdn = "{fqdn}"\n'
        f'mailto = "{global_cfg["mailto"]}"\n'
        f'timezone = "{timezone}"\n'
        f'root_password = "{root_password_hash}"\n'
        f"\n"
        f"[network]\n"
        f'source = "from-answer"\n'
        f'cidr = "{cidr}"\n'
        f'gateway = "{gateway}"\n'
        f'dns = "{dns}"\n'
        f'filter = {{ ID_NET_NAME_MAC = "*{mgmt_mac}" }}\n'
        f"\n"
        f"[disk-setup]\n"
        f'filesystem = "zfs"\n'
        f'filter = {{ ID_SERIAL = "*{boot_disk_serial}*" }}\n'
        f'filter-match = "all"\n'
        f"\n"
        f"[disk-setup.zfs]\n"
        f'raid = "RAID0"\n'
        f"ashift = 12\n"
        f'compress = "zstd"\n'
    )


def _hash_password(plaintext: str) -> str:
    try:
        lib = ctypes.CDLL("libcrypt.so.1", use_errno=True)
        lib.crypt.restype = ctypes.c_char_p
        lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        chars = string.ascii_letters + string.digits + "./"
        salt = "".join(random.choices(chars, k=16))
        setting = f"$6${salt}".encode()
        result = lib.crypt(plaintext.encode(), setting)
        if result:
            return result.decode()
        raise RuntimeError(f"crypt() returned null; errno={ctypes.get_errno()}")
    except OSError as exc:
        raise RuntimeError(f"libcrypt.so.1 unavailable: {exc}") from exc
