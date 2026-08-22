from __future__ import annotations

import ipaddress
from contextlib import nullcontext
from pathlib import Path

from .. import op_secrets
from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    FileSpec,
    run_module_deploy,
    tmpfs_secret_stage,
    validate_secret_reference,
    write_file_map,
)
from ..output import print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-http-boot"
SECRET_NAME = "pve-http-boot-token"
HTTP_BOOT_CONFIG_DIR = "/etc/homelab-http-boot"

# The boot menu is no longer authored here. `proxmox-auto-install-assistant
# prepare-iso --pxe --pxe-loader ipxe` emits a stock boot.ipxe next to
# vmlinuz/initrd.img, and pve-http-boot-autoupdate installs it verbatim, so the
# menu entries and their kernel command lines are always the ones Proxmox ships
# for the exact ISO being served. The hand-rolled menus that used to live in
# configs/ had silently drifted onto pre-8.2 kernel args (proxtui/proxdebug),
# which an installer ignores rather than rejects.
#
# That leaves exactly one iPXE file for the deploy to own: the UEFI HTTP Boot
# entry point, which must be rendered because it bakes in the per-host server IP.
IPXE_ENTRY_TEMPLATES = [
    "httpboot-autoexec.ipxe",
]

OPERATIONAL_SCRIPTS = [
    "pve-http-boot-enable",
    "pve-http-boot-disable",
    "pve-http-boot-autoupdate",
]

STATIC_UNITS = [
    "pve-http-boot-autoupdate.service",
]

FILE_SPECS = (
    FileSpec("http-boot-mgmt.conf", f"{HTTP_BOOT_CONFIG_DIR}/http-boot-mgmt.conf", "600"),
    FileSpec("nginx-http-boot.conf", "/etc/nginx/sites-available/http-boot"),
    FileSpec("httpboot-autoexec.ipxe", "/srv/httpboot/httpboot/autoexec.ipxe"),
    FileSpec("pve-http-boot-enable", "/usr/local/sbin/pve-http-boot-enable", "755"),
    FileSpec("pve-http-boot-disable", "/usr/local/sbin/pve-http-boot-disable", "755"),
    FileSpec("pve-http-boot-autoupdate", "/usr/local/sbin/pve-http-boot-autoupdate", "755"),
    FileSpec(
        "pve-http-boot-autoupdate.service",
        "/etc/systemd/system/pve-http-boot-autoupdate.service",
    ),
    FileSpec(
        "pve-http-boot-autoupdate.timer",
        "/etc/systemd/system/pve-http-boot-autoupdate.timer",
    ),
)


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
        "pve-http-boot",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    configs_dir = root / "pve-http-boot" / "configs"
    templates_dir = root / "pve-http-boot" / "templates"

    for name in OPERATIONAL_SCRIPTS + STATIC_UNITS:
        path = configs_dir / name
        if not path.is_file():
            raise ValueError(f"missing required config: {path}")

    required_templates = (
        "http-boot-mgmt.conf",
        "nginx-http-boot.conf",
        "pve-http-boot-autoupdate.timer",
        *IPXE_ENTRY_TEMPLATES,
    )
    for tmpl in required_templates:
        path = templates_dir / tmpl
        if not path.is_file():
            raise ValueError(f"missing required template: {path}")

    # Validate the catalog entry without rendering the secret during dry-runs/validation.
    try:
        validate_secret_reference(root, SECRET_NAME)
    except op_secrets.OpSecretsError as exc:
        raise ValueError(str(exc)) from exc


def _read_token(root: Path) -> str:
    """Read the PDM answer-auth token from the rendered 1Password secret."""
    env_path = op_secrets.secret_file(root, SECRET_NAME)
    env = op_secrets.parse_env_file(env_path)
    token = env.get("PVE_HTTP_BOOT_TOKEN", "").strip()
    if not token:
        raise ValueError(
            f"PVE_HTTP_BOOT_TOKEN is empty in rendered secret '{SECRET_NAME}'"
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


def normalize_mgmt_ip(value: object, host: str) -> str:
    """Return a bare dotted-quad IPv4 address, or raise.

    The old check only counted dots, which let three broken forms through:
    a CIDR suffix, out-of-range octets, and non-numeric octets. The CIDR case is
    the realistic one — `pve-postinstall.interfaces.mgmt_ip` in this same
    hosts.conf *is* a CIDR, so copying a value between the two identically-named
    keys is an easy mistake that this key must reject.

    It matters because the value is baked into `set http-boot-server` in the iPXE
    entry points. A bad address does not fail at deploy time; it fails when
    someone tries to netboot a bare-metal node, which is exactly when nothing else
    is available to debug it.
    """
    text = str(value).strip()
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError as exc:
        raise ValueError(
            f"pve-http-boot.mgmt_ip must be a bare dotted IPv4 address for {host} "
            f"(got {text!r}); a CIDR suffix or hostname will render an unbootable "
            "iPXE entry point"
        ) from exc


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)

    mgmt_ip = normalize_mgmt_ip(registry.get(host, "pve-http-boot.mgmt_ip"), host)
    pdm_url = str(registry.get(host, "pve-http-boot.pdm_url"))
    pdm_cert_fingerprint = _read_pdm_cert_fingerprint(root)
    autoupdate_schedule = str(
        registry.get(host, "pve-http-boot.autoupdate_schedule", "*-*-* 09:00:00")
    )

    configs_dir = root / "pve-http-boot" / "configs"
    templates_dir = root / "pve-http-boot" / "templates"
    build_dir = root / "pve-http-boot" / "build" / host
    prepare_build_dir(build_dir)

    # Copy operational scripts and service unit
    copy_files(configs_dir, build_dir, OPERATIONAL_SCRIPTS + STATIC_UNITS)

    # Render host-specific templates
    render_file(
        templates_dir / "http-boot-mgmt.conf",
        build_dir / "http-boot-mgmt.conf",
        MGMT_IP=mgmt_ip,
        PDM_URL=pdm_url,
        PDM_CERT_FINGERPRINT=pdm_cert_fingerprint,
    )
    render_file(
        templates_dir / "nginx-http-boot.conf",
        build_dir / "nginx-http-boot.conf",
        MGMT_IP=mgmt_ip,
    )
    render_file(
        templates_dir / "pve-http-boot-autoupdate.timer",
        build_dir / "pve-http-boot-autoupdate.timer",
        AUTOUPDATE_SCHEDULE=autoupdate_schedule,
    )
    for entry in IPXE_ENTRY_TEMPLATES:
        render_file(
            templates_dir / entry,
            build_dir / entry,
            MGMT_IP=mgmt_ip,
        )
    write_file_map(build_dir, FILE_SPECS)

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )

    use_secret_stage = not dry_run and not op_secrets.offline_mode()
    secret_context = (
        tmpfs_secret_stage("homelab-pve-http-boot.")
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
        elif op_secrets.offline_mode():
            print_sub("[offline] token resolution skipped")

        print_sub("Comparing with remote configs...")
        for message in diff_many(
            connection,
            [(build_dir / spec.build_name, spec.remote_path) for spec in FILE_SPECS],
        ):
            print_sub(message)

        if dry_run:
            print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
            return

        upload_paths = [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-http-boot" / "scripts", f"{REMOTE_ROOT}/scripts"),
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
