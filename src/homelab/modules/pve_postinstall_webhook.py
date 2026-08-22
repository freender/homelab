from __future__ import annotations

import shlex
from pathlib import Path

from .. import op_secrets
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    normalize_bool,
    run_module_deploy,
    tmpfs_secret_stage,
    validate_secret_reference,
)
from ..output import print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-postinstall-webhook"
FEATURE = "pve-postinstall-webhook"
SECRET_NAME = "pve-postinstall-webhook"
TOKEN_ENV_KEY = "PVE_POSTINSTALL_WEBHOOK_TOKEN"
PDM_TOKEN_ENV_KEY = "PDM_DEPLOY_TOKEN"

REQUIRED_SCRIPTS = [
    "homelab-postinstall-webhook.py",
    "homelab-pdm-installation-watch.py",
    "homelab-pdm-refresh-remote.py",
    "homelab-postinstall-deploy.sh",
    "homelab-postinstall-webhook.service",
    "homelab-pdm-installation-watch.service",
    "homelab-pdm-installation-watch.timer",
    "install.sh",
]


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
        FEATURE,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    scripts_dir = root / "pve-postinstall-webhook" / "scripts"
    for name in REQUIRED_SCRIPTS:
        path = scripts_dir / name
        if not path.is_file():
            raise ValueError(f"missing required script: {path}")

    try:
        validate_secret_reference(root, SECRET_NAME)
    except op_secrets.OpSecretsError as exc:
        raise ValueError(str(exc)) from exc


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    listen_host = str(registry.get(host, f"{FEATURE}.listen_host", "0.0.0.0"))
    listen_port = str(registry.get(host, f"{FEATURE}.listen_port", "9443"))
    repo_dir = str(registry.get(host, f"{FEATURE}.repo_dir", "/root/homelab"))
    webhook_dry_run_enabled = normalize_webhook_dry_run(registry, host)
    webhook_dry_run = "true" if webhook_dry_run_enabled else "false"
    ssh_timeout = str(registry.get(host, f"{FEATURE}.ssh_timeout_seconds", "1200"))
    deploy_timeout = str(registry.get(host, f"{FEATURE}.deploy_timeout_seconds", "3600"))

    build_dir = root / "pve-postinstall-webhook" / "build" / host
    prepare_build_dir(build_dir)

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )

    scripts_dir = root / "pve-postinstall-webhook" / "scripts"
    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [
            (
                scripts_dir / "homelab-postinstall-webhook.py",
                "/usr/local/sbin/homelab-postinstall-webhook",
            ),
            (
                scripts_dir / "homelab-postinstall-deploy.sh",
                "/usr/local/sbin/homelab-postinstall-deploy",
            ),
            (
                scripts_dir / "homelab-pdm-refresh-remote.py",
                "/usr/local/sbin/homelab-pdm-refresh-remote",
            ),
            (
                scripts_dir / "homelab-pdm-installation-watch.py",
                "/usr/local/sbin/homelab-pdm-installation-watch",
            ),
            (
                scripts_dir / "homelab-postinstall-webhook.service",
                "/etc/systemd/system/homelab-postinstall-webhook.service",
            ),
            (
                scripts_dir / "homelab-pdm-installation-watch.service",
                "/etc/systemd/system/homelab-pdm-installation-watch.service",
            ),
            (
                scripts_dir / "homelab-pdm-installation-watch.timer",
                "/etc/systemd/system/homelab-pdm-installation-watch.timer",
            ),
        ],
    ):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would install post-install webhook listener on {host}:{listen_port}")
        mode = "dry-run" if webhook_dry_run == "true" else "real deploy"
        print_sub(f"[DRY-RUN] Webhook deploy mode: {mode}")
        return

    token, pdm_token = _read_tokens(root)
    with tmpfs_secret_stage("homelab-pve-postinstall-webhook.") as secret_dir:
        env_path = secret_dir / "env"
        _write_env(
            env_path,
            _env_values(
                listen_host,
                listen_port,
                repo_dir,
                token,
                pdm_token,
                webhook_dry_run,
                ssh_timeout,
                deploy_timeout,
            ),
        )
        stage_and_run_remote_installer(
            root,
            connection,
            REMOTE_ROOT,
            [
                (scripts_dir, f"{REMOTE_ROOT}/scripts"),
                (build_dir, f"{REMOTE_ROOT}/build/{host}"),
                (env_path, f"{REMOTE_ROOT}/build/{host}/env"),
            ],
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib", "scripts"),
        )


def normalize_webhook_dry_run(registry, host: str) -> bool:
    return normalize_bool(
        registry.get(host, f"{FEATURE}.dry_run", None),
        True,
        f"{FEATURE}.dry_run must be true or false for {host}",
    )


def _env_values(
    listen_host: str,
    listen_port: str,
    repo_dir: str,
    token: str,
    pdm_token: str,
    webhook_dry_run: str,
    ssh_timeout: str,
    deploy_timeout: str,
) -> dict[str, str]:
    return {
        "LISTEN_HOST": listen_host,
        "LISTEN_PORT": listen_port,
        "REPO_DIR": repo_dir,
        "WEBHOOK_TOKEN": token,
        "DRY_RUN": webhook_dry_run,
        "SSH_TIMEOUT_SECONDS": ssh_timeout,
        "DEPLOY_TIMEOUT_SECONDS": deploy_timeout,
        "SSH_AUTH_SOCK": "/root/.ssh/agent.sock",
        "PDM_BASE_URL": "https://127.0.0.1:8443",
        "PDM_TOKEN_ID": "root@pam!homelab-deploy",
        "PDM_TOKEN_SECRET": pdm_token,
        "PDM_TOKEN_REF": "op://Homelab/PDM Deploy API Token/password",
        "PDM_REMOTE_REFRESH": "true",
        "PDM_REMOTE_AUTHID": "root@pam!pdm-rasputin",
        "PDM_REMOTE_TOKEN_COMMENT": "PDM on arc",
        "OP_BIN": "/root/.local/bin/op",
        "OP_SERVICE_ACCOUNT_TOKEN_FILE": "/root/.config/op/service-account-token",
    }


def _read_tokens(root: Path) -> tuple[str, str]:
    path = op_secrets.secret_file(root, SECRET_NAME)
    env = op_secrets.parse_env_file(path)
    token = env.get(TOKEN_ENV_KEY, "").strip()
    if not token:
        raise ValueError(f"{TOKEN_ENV_KEY} is empty in rendered secret '{SECRET_NAME}'")
    pdm_token = env.get(PDM_TOKEN_ENV_KEY, "").strip()
    if not pdm_token:
        raise ValueError(f"{PDM_TOKEN_ENV_KEY} is empty in rendered secret '{SECRET_NAME}'")
    return token, pdm_token


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={shlex.quote(value)}" for key, value in values.items()]
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    path.chmod(0o600)
