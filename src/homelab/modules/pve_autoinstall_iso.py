"""pve-autoinstall-iso - Build host-specific baked PVE install ISOs via saint.

Renders per-host TOML answer files (with sha512crypt-hashed root passwords from
1Password) and stages them to the pve-pxe host (saint). Also installs the
iso-autobuild weekly timer that rebuilds ISOs whenever the base PVE ISO changes.

Secret handling:
  Answer files are rendered by riven (with hashed passwords), staged to saint
  at /etc/saint/iso-answers/<host>.toml (mode 0600), and consumed by the weekly
  iso-autobuild timer without any further 1Password access.

--force:         Always re-render answer files from 1Password (refresh hashes).
Without --force: If all answer files exist on saint, skip 1Password rendering
                 and only (re)install the script/timer/nginx config.
"""

from __future__ import annotations

import ctypes
import random
import string
import tempfile
from pathlib import Path
from typing import Any

from .. import op_secrets
from ..build import copy_file, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_error, print_ok, print_sub, print_warn
from ..ssh import HostConnection, offline_mode
from .pve_autoinstall import (
    _get_mgmt_mac,
    _is_pdm_host,
    _read_secret_field,
    _root_password_secret,
)

REMOTE_ROOT = "/tmp/homelab-pve-autoinstall-iso"
ANSWER_DIR = "/etc/saint/iso-answers"
ISO_SERVE_DIR = "/srv/pxe/iso"
ISO_BASE_DIR = "/root/iso"


# ---------------------------------------------------------------------------
# Module entry points
# ---------------------------------------------------------------------------

def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)

    # Find the pve-pxe build/serve host (saint).
    pxe_hosts = registry.list_hosts(feature="pve-pxe")
    if not pxe_hosts:
        print_action("Skipping pve-autoinstall-iso (no pve-pxe host configured)")
        return 0
    build_host = pxe_hosts[0]

    # Find the PDM host (rasputin) for shared global config (mailto/keyboard/country).
    pdm_hosts = [
        h for h in registry.list_hosts(feature="pve-autoinstall")
        if _is_pdm_host(registry, h)
    ]
    if not pdm_hosts:
        print_error("pve-autoinstall-iso: no PDM host found; cannot load global config")
        return 1
    pdm_host = pdm_hosts[0]

    # Find ISO target hosts.
    iso_hosts = [
        h for h in registry.list_hosts(feature="pve-autoinstall")
        if not _is_pdm_host(registry, h) and _wants_iso(registry, h)
    ]
    iso_hosts = registry.filter_hosts(requested_host, iso_hosts)

    # If a specific host is requested that is the build host itself, deploy there.
    if not iso_hosts and requested_host not in ("all", "", None):
        if requested_host != build_host:
            print_action(f"Skipping pve-autoinstall-iso (not applicable to {requested_host})")
            return 0

    try:
        validate(root, registry, pdm_host, iso_hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    global_cfg = _get_iso_global_cfg(registry, pdm_host)

    print_action("PVE Baked ISO Build")
    print_sub(f"Build host: {build_host}")
    print_sub(f"ISO targets: {' '.join(iso_hosts) if iso_hosts else '(none)'}")
    print()

    connection = HostConnection(
        build_host,
        user=str(registry.get(build_host, "config.user")),
        hostname=str(registry.get(build_host, "config.hostname")),
    )

    # Determine which answer files need (re)rendering.
    if offline_mode():
        missing: list[str] = iso_hosts
        print_sub("[offline] assuming all answer files missing")
    else:
        missing = _check_missing_answer_files(connection, iso_hosts)

    need_render = force or bool(missing)

    if need_render and not offline_mode():
        if force:
            to_render = iso_hosts
            print_sub("--force: re-rendering all answer files from 1Password")
        else:
            to_render = missing
            print_sub(f"Missing answer files: {' '.join(missing)}; rendering from 1Password")
    elif not need_render:
        to_render = []
        print_sub("All answer files present on saint; skipping 1Password render (use --force to refresh)")
    else:
        to_render = iso_hosts

    # Build local artifacts.
    build_dir = root / "pve-autoinstall-iso" / "build" / build_host
    prepare_build_dir(build_dir)

    module_dir = root / "pve-autoinstall-iso"
    templates_dir = module_dir / "templates"
    scripts_dir = module_dir / "scripts"

    # Copy static script.
    copy_file(scripts_dir / "iso-autobuild", build_dir / "iso-autobuild")

    # Copy static service unit.
    copy_file(templates_dir / "iso-autobuild.service", build_dir / "iso-autobuild.service")

    # Render timer with schedule from hosts.conf (default: Sunday 03:30).
    iso_build_schedule = str(
        registry.get(build_host, "pve-pxe.iso_build_schedule", "Sun *-*-* 03:30:00")
    )
    render_file(
        templates_dir / "iso-autobuild.timer",
        build_dir / "iso-autobuild.timer",
        ISO_BUILD_SCHEDULE=iso_build_schedule,
    )

    # Render TOML answer files for hosts that need it.
    answer_dir = build_dir / "answers"
    answer_dir.mkdir(parents=True, exist_ok=True)

    if to_render and not offline_mode():
        print_sub("Rendering answer files...")
        try:
            for host in to_render:
                secret_name = _root_password_secret(registry, host)
                plaintext = _read_secret_field(root, secret_name, "PVE_ROOT_PASSWORD")
                pw_hash = _hash_password(plaintext)
                toml = _build_iso_answer_toml(registry, host, global_cfg, pw_hash)
                answer_file = answer_dir / f"{host}.toml"
                answer_file.write_text(toml, encoding="utf-8")
                answer_file.chmod(0o600)
                print_sub(f"  rendered {host}.toml")
        except (op_secrets.OpSecretsError, ValueError, OSError) as exc:
            print_error(str(exc))
            return 1
    elif to_render and offline_mode():
        print_sub("[offline] skipping answer file rendering")

    if dry_run:
        print_sub(f"[DRY-RUN] Would stage to {build_host}:{REMOTE_ROOT}/")
        print_sub(f"  answer files: {', '.join(to_render) if to_render else '(none — all present)'}")
        print_sub("  iso-autobuild script + timer + nginx /iso/ location")
        return 0

    if offline_mode():
        print_sub("[offline] skipping remote execution")
        return 0

    session.run(
        lambda host: _deploy_to_build_host(root, build_dir, connection, host, force, to_render),
        [build_host],
    )
    return 0 if session.finish() else 1


def validate(
    root: Path,
    registry: Any = None,
    pdm_host: str | None = None,
    iso_hosts: list[str] | None = None,
) -> None:
    if registry is None:
        registry = default_registry(root)

    if pdm_host is None:
        pdm_hosts = [
            h for h in registry.list_hosts(feature="pve-autoinstall")
            if _is_pdm_host(registry, h)
        ]
        if not pdm_hosts:
            return
        pdm_host = pdm_hosts[0]

    for key in ("mailto", "keyboard", "country"):
        try:
            registry.get(pdm_host, f"pve-autoinstall.{key}")
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.{key} missing for PDM host {pdm_host}")

    if iso_hosts is None:
        iso_hosts = [
            h for h in registry.list_hosts(feature="pve-autoinstall")
            if not _is_pdm_host(registry, h) and _wants_iso(registry, h)
        ]

    for host in iso_hosts:
        for key in ("boot_disk_serial",):
            try:
                registry.get(host, f"pve-autoinstall.{key}")
            except HostLookupError:
                raise ValueError(f"pve-autoinstall.{key} missing for ISO host {host}")
        _get_mgmt_mac(registry, host)


# ---------------------------------------------------------------------------
# Remote execution
# ---------------------------------------------------------------------------

def _deploy_to_build_host(
    root: Path,
    build_dir: Path,
    connection: HostConnection,
    build_host: str,
    force: bool,
    rendered_hosts: list[str],
) -> None:
    print_sub("Staging bundle to saint...")
    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{build_host}"),
            (root / "pve-autoinstall-iso" / "scripts", f"{REMOTE_ROOT}/scripts"),
            (root / "pve-autoinstall-iso" / "templates", f"{REMOTE_ROOT}/templates"),
        ],
        "scripts/install.sh",
        build_host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib", "templates"),
    )

    if rendered_hosts:
        print_sub(f"Answer files staged for: {' '.join(rendered_hosts)}")
    print_ok("ISO build infrastructure deployed to saint")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wants_iso(registry: Any, host: str) -> bool:
    try:
        return bool(registry.get(host, "pve-autoinstall.iso_build"))
    except HostLookupError:
        return False


def _get_iso_global_cfg(registry: Any, pdm_host: str) -> dict:
    """Load shared global fields (mailto/keyboard/country) from the PDM host config."""
    cfg: dict = {}
    for key in ("mailto", "keyboard", "country"):
        try:
            cfg[key] = str(registry.get(pdm_host, f"pve-autoinstall.{key}"))
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.{key} missing for PDM host {pdm_host}")
    return cfg


def _check_missing_answer_files(connection: HostConnection, hosts: list[str]) -> list[str]:
    """Return the subset of hosts that don't yet have a staged answer TOML on saint."""
    try:
        result = connection.connection.run(
            f"ls {ANSWER_DIR}/ 2>/dev/null || true",
            hide=True,
        )
        existing = set(result.stdout.strip().split()) if result.stdout.strip() else set()
    except Exception:
        existing = set()
    return [h for h in hosts if f"{h}.toml" not in existing]


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
    registry: Any,
    host: str,
    global_cfg: dict,
    root_password_hash: str,
) -> str:
    """Render a proxmox-auto-install-assistant TOML answer file for host."""
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

    keyboard = global_cfg["keyboard"]
    country = global_cfg["country"]
    mailto = global_cfg["mailto"]

    return (
        f"# Auto-generated by homelab pve-autoinstall-iso. Do not edit manually.\n"
        f"# Redeploy with: ./deploy --force pve-autoinstall-iso all\n"
        f"\n"
        f"[global]\n"
        f'keyboard = "{keyboard}"\n'
        f'country = "{country}"\n'
        f'fqdn = "{fqdn}"\n'
        f'mailto = "{mailto}"\n'
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
    """Hash a plaintext password with sha512crypt via libcrypt.so.1."""
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
