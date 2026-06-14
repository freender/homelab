from __future__ import annotations

import ctypes
import random
import string
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .. import op_secrets
from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, tmpfs_secret_stage, validate_secret_reference, write_file_map
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
POSTINSTALL_WEBHOOK_SECRET_NAME = "pve-postinstall-webhook"
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
    "httpboot-autoexec.ipxe",
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

FILE_SPECS = (
    FileSpec("pxe-mgmt.conf", f"{PXE_CONFIG_DIR}/pxe-mgmt.conf", "600"),
    FileSpec("nginx-pxe.conf", "/etc/nginx/sites-available/pxe"),
    FileSpec("boot.ipxe", "/srv/pxe/boot.ipxe"),
    FileSpec("pdm-auto-warning.ipxe", "/srv/pxe/pdm-auto-warning.ipxe"),
    FileSpec("pdm-auto.ipxe", "/srv/pxe/pdm-auto.ipxe"),
    FileSpec("pve-load.ipxe", "/srv/pxe/pve-load.ipxe"),
    FileSpec("pve-tui.ipxe", "/srv/pxe/pve-tui.ipxe"),
    FileSpec("pve-gui.ipxe", "/srv/pxe/pve-gui.ipxe"),
    FileSpec("pve-debug.ipxe", "/srv/pxe/pve-debug.ipxe"),
    FileSpec("pve-serial.ipxe", "/srv/pxe/pve-serial.ipxe"),
    FileSpec("httpboot-autoexec.ipxe", "/srv/pxe/httpboot/autoexec.ipxe"),
    FileSpec("pxe-enable", "/usr/local/sbin/pxe-enable", "755"),
    FileSpec("pxe-disable", "/usr/local/sbin/pxe-disable", "755"),
    FileSpec("pxe-autoupdate", "/usr/local/sbin/pxe-autoupdate", "755"),
    FileSpec("iso-autobuild", "/usr/local/sbin/iso-autobuild", "755"),
    FileSpec("pxe-autoupdate.service", "/etc/systemd/system/pxe-autoupdate.service"),
    FileSpec("pxe-autoupdate.timer", "/etc/systemd/system/pxe-autoupdate.timer"),
    FileSpec("iso-autobuild.service", "/etc/systemd/system/iso-autobuild.service"),
)


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

    for tmpl in ("pxe-mgmt.conf", "nginx-pxe.conf", "pxe-autoupdate.timer"):
        path = templates_dir / tmpl
        if not path.is_file():
            raise ValueError(f"missing required template: {path}")

    # Validate the catalog entry without rendering the secret during dry-runs/validation.
    try:
        validate_secret_reference(root, SECRET_NAME)
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

    ip_parts = mgmt_ip.split(".")
    if len(ip_parts) != 4:
        raise ValueError(f"pve-pxe.mgmt_ip must be a dotted IPv4 address for {host}")

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
        templates_dir / "nginx-pxe.conf",
        build_dir / "nginx-pxe.conf",
        MGMT_IP=mgmt_ip,
    )
    render_file(
        templates_dir / "pxe-autoupdate.timer",
        build_dir / "pxe-autoupdate.timer",
        AUTOUPDATE_SCHEDULE=autoupdate_schedule,
    )
    write_file_map(build_dir, FILE_SPECS)

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )

    use_secret_stage = not dry_run and not op_secrets.offline_mode()
    secret_context = (
        tmpfs_secret_stage("homelab-pve-pxe.")
        if use_secret_stage
        else nullcontext(None)
    )

    with secret_context as secret_dir:
        if secret_dir is not None:
            token = _read_token(root)
            token_file = secret_dir / "homelab-pve-auto-install.token"
            token_file.write_text(token, encoding="utf-8")
            token_file.chmod(0o600)
            print_sub("Token resolved from 1Password")
            iso_build_dir = secret_dir
        else:
            if op_secrets.offline_mode():
                print_sub("[offline] token resolution skipped")
            iso_build_dir = build_dir

        rendered_iso_hosts = _render_iso_answers(
            root,
            registry,
            iso_build_dir,
            connection,
            force=force,
            dry_run=dry_run,
        )

        print_sub("Comparing with remote configs...")
        for message in diff_many(
            connection,
            [(build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS],
        ):
            print_sub(message)

        if dry_run:
            print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
            if rendered_iso_hosts:
                print_sub(
                    "[DRY-RUN] Would stage baked ISO answers: "
                    f"{' '.join(rendered_iso_hosts)}"
                )
            return

        upload_paths = [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-pxe" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ]
        if secret_dir is not None:
            upload_paths.insert(1, (secret_dir, f"{REMOTE_ROOT}/build/{host}"))

        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            upload_paths,
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
    for key in ("mailto", "keyboard", "country", "root_ssh_key"):
        try:
            cfg[key] = str(registry.get(pdm_host, f"pve-autoinstall.{key}"))
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.{key} missing for PDM host {pdm_host}")
    try:
        cfg["post_hook_base_url"] = str(
            registry.get(pdm_host, "pve-autoinstall.post_hook_base_url")
        )
    except HostLookupError:
        pass
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
        postinstall_token = ""
        if global_cfg.get("post_hook_base_url"):
            postinstall_token = _read_secret_field(
                root,
                POSTINSTALL_WEBHOOK_SECRET_NAME,
                "PVE_POSTINSTALL_WEBHOOK_TOKEN",
            )
        toml = _build_iso_answer_toml(
            root,
            registry,
            iso_host,
            global_cfg,
            pw_hash,
            postinstall_token,
        )
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
    postinstall_token: str,
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

    toml = (
        f"# Auto-generated by homelab pve-pxe. Do not edit manually.\n"
        f"# Redeploy with: ./deploy --force pve-pxe arc\n"
        f"\n"
        f"[global]\n"
        f'keyboard = "{global_cfg["keyboard"]}"\n'
        f'country = "{global_cfg["country"]}"\n'
        f'fqdn = "{fqdn}"\n'
        f'mailto = "{global_cfg["mailto"]}"\n'
        f'timezone = "{timezone}"\n'
        f'root-password-hashed = "{root_password_hash}"\n'
        f'root-ssh-keys = ["{global_cfg["root_ssh_key"]}"]\n'
        f'reboot-mode = "reboot"\n'
        f'reboot-on-error = false\n'
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
        f'raid = "raid0"\n'
        f"ashift = 12\n"
        f'compress = "zstd"\n'
    )

    post_hook_base_url = global_cfg.get("post_hook_base_url", "")
    if post_hook_base_url:
        toml += (
            f"\n"
            f"[post-installation-webhook]\n"
            f'url = "{post_hook_base_url}"\n'
            f'auth-token = "{postinstall_token}"\n'
        )
        if post_hook_base_url.startswith("https://"):
            cert_fingerprint = _read_pdm_cert_fingerprint(root)
            toml += f'cert-fingerprint = "{cert_fingerprint}"\n'

    return toml


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
