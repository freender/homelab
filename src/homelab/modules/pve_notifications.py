from __future__ import annotations

from pathlib import Path

from .. import op_secrets
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    copy_cached_secret,
    normalize_bool,
    normalize_string_list,
    run_module_deploy,
    tmpfs_secret_stage,
)
from ..output import print_sub
from ..ssh import HostConnection, build_files

MODULE_DIR = "pve-notifications"
REMOTE_ROOT = "/tmp/homelab-pve-notifications"
TELEGRAM_SECRET = "telegram"

NOTIFY_TARGETS = ("alertmanager", "telegram")

# Per-target defaults so a host only has to declare `target:` to switch pipelines.
TARGET_DEFAULTS = {
    "alertmanager": {
        "target_name": "Alertmanager",
        "matcher_name": "alertmanager-matcher",
        "matcher_comment": "Route notifications to Alertmanager",
    },
    "telegram": {
        "target_name": "Telegram",
        "matcher_name": "telegram-matcher",
        "matcher_comment": "Route all notifications to Telegram",
    },
}


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
        MODULE_DIR,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    install_script = root / MODULE_DIR / "scripts" / "install.sh"
    if not install_script.is_file():
        raise ValueError(f"missing install script: {install_script}")

    registry = default_registry(root)
    plans = {}
    for host in hosts:
        if str(registry.get(host, "config.type")) != "pve":
            raise ValueError(f"{MODULE_DIR} supports PVE hosts only: {host}")
        plans[host] = normalize_plan(root, host)

    # Only the Telegram pipeline needs the bot secret; the Alertmanager webhook is
    # an unauthenticated POST to the LAN-local Alertmanager.
    if any(plan["notify_target"] == "telegram" for plan in plans.values()):
        secret = op_secrets.secret_file(root, TELEGRAM_SECRET)
        env = op_secrets.parse_env_file(secret)
        for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHATID"):
            if not env.get(key, "").strip():
                raise ValueError(f"{TELEGRAM_SECRET}: {key} is empty")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "pve":
        raise ValueError(f"{MODULE_DIR} supports PVE hosts only: {host}")

    plan = normalize_plan(root, host)
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)
    write_plan(build_dir / "notification-plan.conf", plan)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy PVE notifications to {host}:{REMOTE_ROOT}/")
        print_sub(f"Notification target: {plan['notify_target']}")
        if plan["notify_target"] == "alertmanager":
            print_sub(f"Alertmanager URL: {plan['alertmanager_url']}")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    uploads = [
        (build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]

    if plan["notify_target"] != "telegram":
        stage_and_run_remote_installer(
            root,
            HostConnection(host),
            REMOTE_ROOT,
            uploads,
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib", "scripts"),
        )
        return

    with tmpfs_secret_stage("homelab-pve-notifications.") as secret_dir:
        secret_stage = copy_cached_secret(
            root,
            TELEGRAM_SECRET,
            secret_dir / "telegram.env",
        )
        stage_and_run_remote_installer(
            root,
            HostConnection(host),
            REMOTE_ROOT,
            [*uploads, (secret_stage, f"{REMOTE_ROOT}/build/{host}/telegram.env")],
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib", "scripts"),
        )


def normalize_plan(root: Path, host: str) -> dict[str, object]:
    registry = default_registry(root)
    prefix = MODULE_DIR

    notify_target = text_value(registry.get(host, f"{prefix}.target", "telegram")).lower()
    if notify_target not in NOTIFY_TARGETS:
        raise ValueError(
            f"{prefix}.target must be one of {', '.join(NOTIFY_TARGETS)} for {host}"
        )
    defaults = TARGET_DEFAULTS[notify_target]

    alertmanager_url = str(registry.get(host, f"{prefix}.alertmanager_url", "")).strip()
    if notify_target == "alertmanager":
        if not alertmanager_url:
            raise ValueError(f"{prefix}.alertmanager_url is required for {host}")
        if not alertmanager_url.startswith(("http://", "https://")):
            raise ValueError(
                f"{prefix}.alertmanager_url must be an http(s) URL for {host}"
            )

    return {
        "notify_target": notify_target,
        "alertmanager_url": alertmanager_url,
        "alertmanager_severity": text_value(
            registry.get(host, f"{prefix}.alertmanager_severity", "critical")
        ),
        "alertmanager_alertname": text_value(
            registry.get(host, f"{prefix}.alertmanager_alertname", "ProxmoxNotification")
        ),
        "target_name": text_value(
            registry.get(host, f"{prefix}.target_name", defaults["target_name"])
        ),
        "matcher_name": text_value(
            registry.get(host, f"{prefix}.matcher_name", defaults["matcher_name"])
        ),
        "matcher_comment": text_value(
            registry.get(host, f"{prefix}.matcher_comment", defaults["matcher_comment"])
        ),
        "match_severity": normalize_string_list(
            registry.get(host, f"{prefix}.match_severity", ["error"]),
            f"{prefix}.match_severity must be a list for {host}",
        ),
        "disable_mail_to_root": normalize_bool(
            registry.get(host, f"{prefix}.disable_mail_to_root", True),
            True,
            f"{prefix}.disable_mail_to_root must be boolean for {host}",
        ),
        "disable_default_matcher": normalize_bool(
            registry.get(host, f"{prefix}.disable_default_matcher", True),
            True,
            f"{prefix}.disable_default_matcher must be boolean for {host}",
        ),
        "remove_matchers": normalize_string_list(
            registry.get(host, f"{prefix}.remove_matchers", ["backup-errors"]),
            f"{prefix}.remove_matchers must be a list for {host}",
        ),
        "remove_webhook_targets": normalize_string_list(
            registry.get(host, f"{prefix}.remove_webhook_targets", ["telegram"]),
            f"{prefix}.remove_webhook_targets must be a list for {host}",
        ),
    }


def text_value(value: object) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("PVE notification plan values must not be empty")
    return text


def shell_quote(value: object) -> str:
    return str(value).replace("'", "'\"'\"'")


def write_plan(path: Path, plan: dict[str, object]) -> None:
    match_severity = tuple(str(value) for value in plan["match_severity"])
    remove_matchers = tuple(str(value) for value in plan["remove_matchers"])
    remove_webhook_targets = tuple(str(value) for value in plan["remove_webhook_targets"])
    lines = [
        f"NOTIFY_TARGET='{shell_quote(plan['notify_target'])}'",
        f"ALERTMANAGER_URL='{shell_quote(plan['alertmanager_url'])}'",
        f"ALERTMANAGER_SEVERITY='{shell_quote(plan['alertmanager_severity'])}'",
        f"ALERTMANAGER_ALERTNAME='{shell_quote(plan['alertmanager_alertname'])}'",
        f"TARGET_NAME='{shell_quote(plan['target_name'])}'",
        f"MATCHER_NAME='{shell_quote(plan['matcher_name'])}'",
        f"MATCHER_COMMENT='{shell_quote(plan['matcher_comment'])}'",
        f"DISABLE_MAIL_TO_ROOT='{str(plan['disable_mail_to_root']).lower()}'",
        f"DISABLE_DEFAULT_MATCHER='{str(plan['disable_default_matcher']).lower()}'",
        f"MATCH_SEVERITY_COUNT='{len(match_severity)}'",
    ]
    for index, severity in enumerate(match_severity):
        lines.append(f"MATCH_SEVERITY_{index}='{shell_quote(severity)}'")
    lines.append(f"REMOVE_MATCHER_COUNT='{len(remove_matchers)}'")
    for index, matcher in enumerate(remove_matchers):
        lines.append(f"REMOVE_MATCHER_{index}='{shell_quote(matcher)}'")
    lines.append(f"REMOVE_WEBHOOK_TARGET_COUNT='{len(remove_webhook_targets)}'")
    for index, target in enumerate(remove_webhook_targets):
        lines.append(f"REMOVE_WEBHOOK_TARGET_{index}='{shell_quote(target)}'")
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")
