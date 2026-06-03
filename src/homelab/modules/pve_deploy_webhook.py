from __future__ import annotations

from pathlib import Path

from .. import op_secrets
from ..build import copy_file, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-deploy-webhook"
SECRET_NAME = "pve-deploy-webhook"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = "8088"
DEFAULT_HOMELAB_ROOT = "/home/freender/homelab"
DEFAULT_SSH_AUTH_SOCK = "/home/freender/.ssh/agent.sock"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-deploy-webhook")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-deploy-webhook (not applicable to {requested_host})")
        return 0

    try:
        validate(root)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    module_dir = root / "pve-deploy-webhook"
    for path in (
        module_dir / "scripts" / "install.sh",
        module_dir / "scripts" / "homelab-pve-deploy-webhook.py",
        module_dir / "templates" / "homelab-pve-deploy-webhook.service",
    ):
        if not path.is_file():
            raise ValueError(f"missing required file: {path}")

    try:
        op_secrets.secret_file(root, SECRET_NAME)
    except op_secrets.OpSecretsError as exc:
        raise ValueError(str(exc)) from exc


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    cfg_prefix = "pve-deploy-webhook"
    listen_host = str(registry.get(host, f"{cfg_prefix}.listen_host", DEFAULT_LISTEN_HOST))
    listen_port = str(registry.get(host, f"{cfg_prefix}.listen_port", DEFAULT_LISTEN_PORT))
    homelab_root = str(registry.get(host, f"{cfg_prefix}.homelab_root", DEFAULT_HOMELAB_ROOT))
    ssh_auth_sock = str(registry.get(host, f"{cfg_prefix}.ssh_auth_sock", DEFAULT_SSH_AUTH_SOCK))
    allowed_hosts = " ".join(registry.list_hosts(feature="pve-postinstall"))

    module_dir = root / "pve-deploy-webhook"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    copy_file(
        module_dir / "scripts" / "homelab-pve-deploy-webhook.py",
        build_dir / "homelab-pve-deploy-webhook.py",
    )
    render_file(
        module_dir / "templates" / "homelab-pve-deploy-webhook.service",
        build_dir / "homelab-pve-deploy-webhook.service",
    )

    if not op_secrets.offline_mode():
        env = op_secrets.parse_env_file(op_secrets.secret_file(root, SECRET_NAME))
        token = env.get("PVE_DEPLOY_WEBHOOK_TOKEN", "").strip()
        if not token:
            raise ValueError(
                f"PVE_DEPLOY_WEBHOOK_TOKEN is empty in rendered secret '{SECRET_NAME}'"
            )
        (build_dir / "homelab-pve-deploy-webhook.env").write_text(
            "\n".join(
                [
                    f"PVE_DEPLOY_WEBHOOK_TOKEN={token}",
                    f"PVE_DEPLOY_WEBHOOK_HOST={listen_host}",
                    f"PVE_DEPLOY_WEBHOOK_PORT={listen_port}",
                    f"PVE_DEPLOY_WEBHOOK_ALLOWED_HOSTS={allowed_hosts}",
                    f"HOMELAB_ROOT={homelab_root}",
                    f"SSH_AUTH_SOCK={ssh_auth_sock}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (build_dir / "homelab-pve-deploy-webhook.env").chmod(0o600)
        print_sub("Webhook token resolved from 1Password")
    else:
        print_sub("[offline] token resolution skipped")

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )

    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [
            (
                build_dir / "homelab-pve-deploy-webhook.py",
                "/usr/local/sbin/homelab-pve-deploy-webhook",
            ),
            (
                build_dir / "homelab-pve-deploy-webhook.service",
                "/etc/systemd/system/homelab-pve-deploy-webhook.service",
            ),
        ],
    ):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        hostname = registry.get(host, "config.hostname")
        print_sub(f"Webhook URL: http://{hostname}:{listen_port}/pve-postinstall")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (module_dir / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
