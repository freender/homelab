from __future__ import annotations

from pathlib import Path

from .. import op_secrets
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import (
    copy_cached_secret,
    normalize_bool,
    normalize_string_list,
    tmpfs_secret_stage,
)
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files

MODULE_DIR = "pve-notifications"
REMOTE_ROOT = "/tmp/homelab-pve-notifications"
TELEGRAM_SECRET = "telegram"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature=MODULE_DIR)
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping {MODULE_DIR} (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    install_script = root / MODULE_DIR / "scripts" / "install.sh"
    if not install_script.is_file():
        raise ValueError(f"missing install script: {install_script}")

    secret = op_secrets.secret_file(root, TELEGRAM_SECRET)
    env = op_secrets.parse_env_file(secret)
    for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHATID"):
        if not env.get(key, "").strip():
            raise ValueError(f"{TELEGRAM_SECRET}: {key} is empty")

    registry = default_registry(root)
    for host in hosts:
        if str(registry.get(host, "config.type")) != "pve":
            raise ValueError(f"{MODULE_DIR} supports PVE hosts only: {host}")
        normalize_plan(root, host)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "pve":
        raise ValueError(f"{MODULE_DIR} supports PVE hosts only: {host}")

    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)
    write_plan(build_dir / "notification-plan.conf", normalize_plan(root, host))

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy PVE notifications to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
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
            [
                (build_dir, f"{REMOTE_ROOT}/build/{host}"),
                (secret_stage, f"{REMOTE_ROOT}/build/{host}/telegram.env"),
                (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
            ],
            "scripts/install.sh",
            host,
            env=force_env(force),
            require_root=True,
            remote_subdirs=("build", "lib", "scripts"),
        )


def normalize_plan(root: Path, host: str) -> dict[str, object]:
    registry = default_registry(root)
    prefix = MODULE_DIR
    return {
        "target_name": text_value(registry.get(host, f"{prefix}.target_name", "Telegram")),
        "matcher_name": text_value(
            registry.get(host, f"{prefix}.matcher_name", "telegram-matcher")
        ),
        "matcher_comment": text_value(
            registry.get(
                host,
                f"{prefix}.matcher_comment",
                "Route all notifications to Telegram",
            )
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
