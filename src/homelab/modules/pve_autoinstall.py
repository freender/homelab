"""pve-autoinstall - Manage PDM prepared answer configurations from hosts.conf.

Stages a JSON answer plan and a remote Python sync script to the PDM host, then
executes the script over SSH so the PDM API calls happen from localhost (where
the PDM host can reach 10.0.0.50:8443 directly).

Architecture:
  - arc: the PDM host. Configured with pve-autoinstall.pdm_host and
    related fields. The module SSHes here to run sync-answers.py.
  - PVE nodes (ace, bray, clovis, osiris): each has pve-autoinstall.dmi_uuid,
    boot_disk_serial, answer_name. Network/tz/fqdn are derived from existing
    pve-postinstall.interfaces and pve-postinstall.timezone fields.
  - root-ssh-keys and mailto come from the PDM host's pve-autoinstall config.
  - root-password-hashed is generated at deploy time by the remote script
    using the plaintext password from 1Password.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .. import op_secrets
from ..deploy import DeploySession
from ..hosts import HostLookupError, default_registry
from ..output import print_action, print_error, print_ok, print_sub
from ..ssh import HostConnection, offline_mode

PDM_SECRET_NAME = "pdm-deploy-token"
PWD_SECRET_NAME = "pve-root-password"
PWD_ENV_KEY = "PVE_ROOT_PASSWORD"
REMOTE_ROOT = "/tmp/homelab-pve-autoinstall"


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

    pdm_hosts = [
        h for h in registry.list_hosts(feature="pve-autoinstall")
        if _is_pdm_host(registry, h)
    ]
    if not pdm_hosts:
        print_action("Skipping pve-autoinstall (no PDM host with pdm_host configured)")
        return 0
    if len(pdm_hosts) > 1:
        print_error(f"pve-autoinstall: multiple PDM hosts: {pdm_hosts}; expected one")
        return 1

    pdm_host_name = pdm_hosts[0]

    # Collect PVE node hosts to sync.
    pve_hosts = [
        h for h in registry.list_hosts(feature="pve-autoinstall")
        if not _is_pdm_host(registry, h)
    ]
    pve_hosts = registry.filter_hosts(requested_host, pve_hosts)
    if not pve_hosts:
        print_action(f"Skipping pve-autoinstall (not applicable to {requested_host})")
        return 0

    try:
        validate(root, registry, pdm_host_name, pve_hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    try:
        pdm_cfg = _load_pdm_config(root, registry, pdm_host_name)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    print_action("PVE Automated Install (PDM Answers)")
    print_sub(f"PDM host: {pdm_host_name} ({pdm_cfg['pdm_host']}:{pdm_cfg['pdm_port']})")
    print_sub(f"PVE nodes: {' '.join(pve_hosts)}")
    print()

    # Build the answer plan (sans secrets).
    answer_entries = []
    for host in pve_hosts:
        try:
            entry = _build_answer_entry(root, registry, host, pdm_cfg)
            answer_entries.append(entry)
            print_sub(f"  {entry['id']}: {entry['fqdn']} {entry['cidr']}")
        except (ValueError, HostLookupError) as exc:
            print_error(f"Failed to build answer for {host}: {exc}")
            return 1

    plan = {
        "pdm": {
            "host": pdm_cfg["pdm_host"],
            "port": pdm_cfg["pdm_port"],
            "cert_fingerprint": pdm_cfg["pdm_cert_fingerprint"],
            "token_id": pdm_cfg["pdm_token_id"],
        },
        "answers": answer_entries,
    }

    if dry_run:
        print_sub(f"[DRY-RUN] Would stage answer plan and run sync-answers.py on {pdm_host_name}")
        for entry in answer_entries:
            print_sub(
                f"  {entry['id']}: fqdn={entry['fqdn']} cidr={entry['cidr']}"
                f" disk={entry['disk-filter']['ID_SERIAL']}"
                f" uuid={entry['target-filter']['/dmi/system/uuid']}"
            )
        return 0

    if offline_mode():
        print_sub("[offline] skipping remote execution")
        return 0

    # Resolve secrets.
    try:
        pdm_token_secret = _read_secret_field(root, PDM_SECRET_NAME, "PDM_DEPLOY_TOKEN")
        root_passwords = _read_root_passwords(root, registry, pve_hosts)
    except op_secrets.OpSecretsError as exc:
        print_error(str(exc))
        return 1

    # Stage + execute on the PDM host.
    session.run(
        lambda host: _run_on_pdm_host(
            root, registry, pdm_host_name, plan, pdm_token_secret,
            root_passwords, force,
        ),
        [pdm_host_name],
    )
    return 0 if session.finish() else 1


def validate(
    root: Path,
    registry: Any = None,
    pdm_host: str | None = None,
    pve_hosts: list[str] | None = None,
) -> None:
    if registry is None:
        registry = default_registry(root)

    if pdm_host is None:
        pdm_hosts = [
            h for h in registry.list_hosts(feature="pve-autoinstall")
            if _is_pdm_host(registry, h)
        ]
        if not pdm_hosts:
            return  # nothing to validate
        pdm_host = pdm_hosts[0]

    _load_pdm_config(root, registry, pdm_host)

    if pve_hosts is None:
        pve_hosts = [
            h for h in registry.list_hosts(feature="pve-autoinstall")
            if not _is_pdm_host(registry, h)
        ]

    for host in pve_hosts:
        _validate_pve_host(registry, host)

    for secret_name in (PDM_SECRET_NAME,):
        try:
            op_secrets.secret_file(root, secret_name)
        except op_secrets.OpSecretsError as exc:
            raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Remote execution
# ---------------------------------------------------------------------------

def _run_on_pdm_host(
    root: Path,
    registry: Any,
    pdm_host_name: str,
    plan: dict,
    pdm_token_secret: str,
    root_passwords: dict[str, str],
    force: bool,
) -> None:
    connection = HostConnection(
        pdm_host_name,
        user=str(registry.get(pdm_host_name, "config.user")),
        hostname=str(registry.get(pdm_host_name, "config.hostname")),
    )

    # Write plan and combined secrets file to tmpfs.
    tmpdir = Path(tempfile.mkdtemp(prefix="homelab-pve-autoinstall.", dir="/dev/shm"))
    tmpdir.chmod(0o700)
    try:
        plan_path = tmpdir / "answer-plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        # Combined token file (PDM API token + per-host PVE root passwords).
        token_path = tmpdir / "pdm-api-token"
        lines = [f"PDM_DEPLOY_TOKEN={pdm_token_secret}"]
        for host, password in sorted(root_passwords.items()):
            lines.append(f"PVE_ROOT_PASSWORD__{host}={password}")
        token_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        token_path.chmod(0o600)

        script_src = root / "pve-autoinstall" / "scripts" / "sync-answers.py"

        print_sub(f"Staging bundle to {pdm_host_name}...")
        connection.prepare_remote_dir(REMOTE_ROOT)
        connection.upload(plan_path, f"{REMOTE_ROOT}/answer-plan.json")
        connection.upload(token_path, f"{REMOTE_ROOT}/pdm-api-token")
        connection.upload(script_src, f"{REMOTE_ROOT}/sync-answers.py")

        force_flag = " --force" if force else ""
        print_sub(f"Running sync-answers.py on {pdm_host_name}...")
        connection.connection.run(
            f'chmod +x "{REMOTE_ROOT}/sync-answers.py" && '
            f'python3 "{REMOTE_ROOT}/sync-answers.py"{force_flag}',
            pty=False,
        )
        print_ok("PDM answers synced")
    finally:
        # Shred secrets from tmpfs.
        shred = shutil.which("shred")
        for f in tmpdir.glob("*"):
            if shred:
                os.system(f'{shred} -u -n 1 "{f}"')
            else:
                f.unlink(missing_ok=True)
        tmpdir.rmdir()

        # Clean up remote staging dir (contains secrets).
        try:
            connection.connection.run(
                f'rm -rf "{REMOTE_ROOT}"', hide=True, warn=True
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_pdm_host(registry: Any, host: str) -> bool:
    try:
        registry.get(host, "pve-autoinstall.pdm_host")
        return True
    except HostLookupError:
        return False


def _load_pdm_config(root: Path, registry: Any, pdm_host: str) -> dict:
    required_keys = (
        "pdm_host", "pdm_token_id",
        "install_auth_token_name", "root_ssh_key", "mailto", "keyboard", "country",
    )
    cfg: dict = {}
    for key in required_keys:
        try:
            cfg[key] = str(registry.get(pdm_host, f"pve-autoinstall.{key}"))
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.{key} missing for PDM host {pdm_host}")
    try:
        cfg["pdm_cert_fingerprint"] = _read_secret_field(
            root,
            PDM_SECRET_NAME,
            "PDM_CERT_FINGERPRINT",
        )
    except op_secrets.OpSecretsError as exc:
        raise ValueError(str(exc)) from exc
    cfg["pdm_port"] = int(registry.get(pdm_host, "pve-autoinstall.pdm_port", 8443))
    try:
        cfg["post_hook_base_url"] = str(
            registry.get(pdm_host, "pve-autoinstall.post_hook_base_url")
        )
    except HostLookupError:
        pass
    return cfg


def _root_password_secret(registry: Any, host: str) -> str:
    try:
        return str(registry.get(host, "pve-autoinstall.root_password_secret"))
    except HostLookupError:
        return PWD_SECRET_NAME


def _read_root_passwords(root: Path, registry: Any, hosts: list[str]) -> dict[str, str]:
    passwords: dict[str, str] = {}
    cache: dict[str, str] = {}
    for host in hosts:
        secret_name = _root_password_secret(registry, host)
        if secret_name not in cache:
            cache[secret_name] = _read_secret_field(root, secret_name, PWD_ENV_KEY)
        passwords[host] = cache[secret_name]
    return passwords


def _validate_pve_host(registry: Any, host: str) -> None:
    for key in ("dmi_uuid", "boot_disk_serial", "answer_name"):
        try:
            registry.get(host, f"pve-autoinstall.{key}")
        except HostLookupError:
            raise ValueError(f"pve-autoinstall.{key} missing for host {host}")

    # Network: explicit override or pve-postinstall.interfaces.
    has_cidr_override = True
    try:
        registry.get(host, "pve-autoinstall.cidr")
        registry.get(host, "pve-autoinstall.gateway")
    except HostLookupError:
        has_cidr_override = False

    if not has_cidr_override:
        for key in ("interfaces.mgmt_ip", "interfaces.gateway"):
            try:
                registry.get(host, f"pve-postinstall.{key}")
            except HostLookupError:
                raise ValueError(
                    f"pve-postinstall.{key} required for pve-autoinstall on {host} "
                    f"(or set pve-autoinstall.cidr + pve-autoinstall.gateway)"
                )

    _get_mgmt_mac(registry, host)


def _get_mgmt_mac(registry: Any, host: str) -> str:
    try:
        mac = str(registry.get(host, "pve-autoinstall.mgmt_mac"))
        if mac:
            return mac.replace(":", "").lower()
    except HostLookupError:
        pass

    try:
        interfaces = registry.get(host, "pve-interface-pinning.interfaces")
    except HostLookupError:
        raise ValueError(f"pve-interface-pinning.interfaces missing for {host}")
    if not isinstance(interfaces, list):
        raise ValueError(f"pve-interface-pinning.interfaces must be a list for {host}")
    for iface in interfaces:
        if isinstance(iface, dict) and iface.get("role") == "management":
            mac = iface.get("mac", "")
            if not mac:
                raise ValueError(f"management interface has empty mac for {host}")
            return mac.replace(":", "").lower()
    raise ValueError(f"No management interface in pve-interface-pinning for {host}")


def _build_answer_entry(root: Path, registry: Any, host: str, pdm_cfg: dict) -> dict:
    """Build a single PDM prepared answer dict for inclusion in the plan JSON."""
    answer_name = str(registry.get(host, "pve-autoinstall.answer_name"))
    dmi_uuid = str(registry.get(host, "pve-autoinstall.dmi_uuid"))
    boot_disk_serial = str(registry.get(host, "pve-autoinstall.boot_disk_serial"))

    # Network — explicit override takes priority over pve-postinstall.interfaces.
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

    timezone = str(registry.get(host, "pve-postinstall.timezone", "UTC"))
    fqdn = str(registry.get(host, "config.hostname"))
    mgmt_mac = _get_mgmt_mac(registry, host)

    entry = {
        "id": answer_name,
        "_host": host,
        "fqdn": fqdn,
        "keyboard": pdm_cfg["keyboard"],
        "country": pdm_cfg["country"],
        "timezone": timezone,
        "mailto": pdm_cfg["mailto"],
        "filesystem": {
            "filesystem": "zfs",
            "raid": "RAID0",
            "ashift": 12,
            "compress": "zstd",
        },
        "disk-mode": "filter",
        "disk-filter": {"ID_SERIAL": f"*{boot_disk_serial}*"},
        "disk-filter-match": "all",
        "use-dhcp-network": False,
        "use-dhcp-fqdn": False,
        "cidr": cidr,
        "gateway": gateway,
        "dns": dns,
        "netdev-filter": {"ID_NET_NAME_MAC": f"*{mgmt_mac}"},
        "authorized-tokens": [pdm_cfg["install_auth_token_name"]],
        "netif-name-pinning-enabled": True,
        "reboot-mode": "reboot",
        "reboot-on-error": False,
        "is-default": False,
        "target-filter": {
            "/dmi/system/uuid": dmi_uuid,
            "/product/product": "pve",
        },
        "root-ssh-keys": [pdm_cfg["root_ssh_key"]],
        # root-password-hashed is injected by the remote script at runtime.
    }

    post_hook_base_url = pdm_cfg.get("post_hook_base_url")
    if post_hook_base_url:
        entry["post-hook-base-url"] = post_hook_base_url
        if post_hook_base_url.startswith("https://"):
            entry["post-hook-cert-fp"] = pdm_cfg["pdm_cert_fingerprint"]

    return entry


def _read_secret_field(root: Path, secret_name: str, env_key: str) -> str:
    path = op_secrets.secret_file(root, secret_name)
    env = op_secrets.parse_env_file(path)
    value = env.get(env_key, "").strip()
    if not value:
        raise op_secrets.OpSecretsError(
            f"{env_key} is empty in rendered secret '{secret_name}'"
        )
    return value
