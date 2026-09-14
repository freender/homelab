#!/usr/bin/env python3
"""Remote installer for the pve-notifications module (freender/homelab-ops#30).

Points PVE's notification system at one webhook target -- Alertmanager or a
Telegram bot -- through one matcher, and retires the targets and matchers an
earlier pipeline left behind. Nothing is written to disk: the whole deploy is
`pvesh` calls against `/cluster/notifications`, which on a cluster node is
cluster-wide config in pmxcfs, so deploying to `ace` changes it for `bray` and
`clovis` too.

**A converged target is no longer rewritten.** The bash ran `pvesh set` on the
endpoint and the matcher every deploy, bumping the config digest each time. Both
are now read back and compared, and only a difference writes. That is also what
makes the canary meaningful: an unchanged digest is proof nothing was written.
The Telegram endpoint is the exception and is always set, because PVE never
returns secret values, so a rotated bot token is indistinguishable from an
unrotated one.

**`pvesh set` only adds; it never took anything away.** The bash passed the
desired properties and nothing else, so a property this module does not set
survived every deploy:

* Dropping `match_severity` from `hosts.conf` to route *every* severity left the
  old `match-severity` on the matcher -- still filtering, while the deploy
  reported success.
* A `match-field`, `match-calendar`, `invert-match` or `disable` added by hand
  silently narrowed or turned off alerting, and no redeploy would ever undo it.

Those are now deleted as part of the same `set`. This is the fifth "a step did
less than the surrounding code assumed" finding in this port.

**Failures are no longer swallowed.** Removing a stale matcher or target and
disabling `mail-to-root` / `default-matcher` were each `|| true` in the bash, so
a failed disable left root mail flowing while the deploy said "configured".
Each is now checked against the listing first -- absent is fine, a real failure
fails the deploy.

**Stale routes are removed after the new one exists**, not before. The bash
deleted old matchers before creating the new one, leaving a window with no route.

The Alertmanager body is built with `json.dumps` rather than string concatenation,
so an alert name containing a quote can no longer produce invalid JSON. For every
value in inventory today the bytes are identical to what the bash sent.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

from homelab_install import env, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which

TARGETS = ("alertmanager", "telegram")

WEBHOOK_ROOT = "/cluster/notifications/endpoints/webhook"
MATCHER_ROOT = "/cluster/notifications/matchers"
SENDMAIL_ROOT = "/cluster/notifications/endpoints/sendmail"
MAIL_TO_ROOT = "mail-to-root"
DEFAULT_MATCHER = "default-matcher"

TELEGRAM_SECRET_NAME = "telegram.env"
TELEGRAM_URL = "https://api.telegram.org/bot{{ secrets.token }}/sendMessage"

# Properties this module owns on each object but may not set. Any of them present
# on the live object is deleted, because `pvesh set` would otherwise leave it.
ENDPOINT_UNSET = ("disable",)
MATCHER_UNSET = ("disable", "invert-match", "match-calendar", "match-field")

REQUIRED_ENV = ("NOTIFY_TARGET", "TARGET_NAME", "MATCHER_NAME", "MATCHER_COMMENT")
REQUIRED_ALERTMANAGER_ENV = ("ALERTMANAGER_URL", "ALERTMANAGER_ALERTNAME", "ALERTMANAGER_SEVERITY")


def b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def names(ctx: InstallContext, name: str) -> tuple[str, ...]:
    """A space-separated list from the env file. PVE object names and severities
    cannot contain whitespace, and the orchestrator refuses any that do."""
    return tuple(ctx.env.get(name, "").split())


def pvesh(*args: str) -> str:
    result = _run(["pvesh", *args], check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout
    # A parameter-verification failure echoes the offending value, and on the
    # Telegram path that value is the bot token -- which would land in the deploy
    # log. Same reasoning as `main._parse_env_file` not echoing a malformed line.
    if "--secret" in args:
        detail = "(output withheld: the call carried secrets)"
    else:
        detail = (result.stderr or result.stdout or "").strip()
    raise InstallError(f"pvesh {args[0]} {args[1]} failed: {detail}")


def listing(path: str) -> dict[str, dict]:
    """Every object under `path`, by name.

    One listing instead of a `pvesh get` per name: the bash treated *any* failed
    get as "does not exist", so a transient pmxcfs error sent it down the create
    path, which then failed with "already exists".
    """
    entries = json.loads(pvesh("get", path, "--output-format", "json"))
    return {entry["name"]: entry for entry in entries}


def read_secret(path: Path) -> dict[str, str]:
    """Parse the staged Telegram secret the way `op_secrets.parse_env_file` does.

    Not `main._parse_env_file`: that reads the orchestrator's shlex-quoted env
    format, and `op inject` output is unquoted and opens with a comment.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def destroy_secret(path: Path) -> None:
    if not path.exists():
        return
    if _which("shred"):
        _run(["shred", "-u", "-n", "1", str(path)], check=False)
    path.unlink(missing_ok=True)


def alertmanager_endpoint(ctx: InstallContext) -> dict[str, object]:
    """One-shot PVE events are posted as Alertmanager alerts so they share the
    same routing, silences and MWBot alert list as metric-based alerts.
    Alertmanager drops empty labels, so absent PVE fields simply disappear."""
    env.require(ctx, *REQUIRED_ALERTMANAGER_ENV)
    body = [
        {
            "labels": {
                "alertname": ctx.env["ALERTMANAGER_ALERTNAME"],
                "severity": ctx.env["ALERTMANAGER_SEVERITY"],
                "source": "pve",
                "host": "{{ fields.hostname }}",
                "name": "{{ fields.type }}",
                "vmid": "{{ fields.vmid }}",
                "pve_severity": "{{ severity }}",
            },
            "annotations": {
                "summary": "{{ escape title }}",
                "description": "{{ escape message }}",
            },
        }
    ]
    return {
        "url": f"{ctx.env['ALERTMANAGER_URL'].rstrip('/')}/api/v2/alerts",
        "body": b64(json.dumps(body, separators=(",", ":"))),
    }


def telegram_endpoint(ctx: InstallContext) -> tuple[dict[str, object], list[str]]:
    secret_path = ctx.build_dir / TELEGRAM_SECRET_NAME
    if not secret_path.is_file():
        raise InstallError(f"missing Telegram secret: {secret_path}")
    secret = read_secret(secret_path)
    token = secret.get("TELEGRAM_TOKEN", "")
    chat_id = secret.get("TELEGRAM_CHATID", "")
    if not token or not chat_id:
        raise InstallError("TELEGRAM_TOKEN or TELEGRAM_CHATID missing")

    body = {
        "chat_id": "{{ secrets.chat_id }}",
        "text": "{{ escape title }}\n\n{{ escape message }}",
        "parse_mode": "Markdown",
    }
    desired = {"url": TELEGRAM_URL, "body": b64(json.dumps(body, separators=(",", ":")))}
    secrets = [f"name=token,value={b64(token)}", f"name=chat_id,value={b64(chat_id)}"]
    return desired, secrets


def stale_properties(current: dict, unset: tuple[str, ...]) -> list[str]:
    return [key for key in unset if key in current]


def differs(current: dict, desired: dict[str, object]) -> bool:
    return any(current.get(key) != value for key, value in desired.items())


def set_args(desired: dict[str, object], delete: list[str]) -> list[str]:
    """Render `desired` as pvesh options; a list becomes a repeated option."""
    args: list[str] = []
    for key, value in desired.items():
        for item in value if isinstance(value, list) else [value]:
            args += [f"--{key}", str(item)]
    if delete:
        args += ["--delete", ",".join(delete)]
    return args


def secret_args(secrets: list[str]) -> list[str]:
    return [arg for secret in secrets for arg in ("--secret", secret)]


def configure_endpoint(ctx: InstallContext, target: str, name: str) -> None:
    desired: dict[str, object] = {
        "method": "post",
        "header": [f"name=Content-Type,value={b64('application/json')}"],
    }
    secrets: list[str] = []
    if target == "alertmanager":
        desired.update(alertmanager_endpoint(ctx))
        log.action(f"Configuring Alertmanager webhook target {name} -> {desired['url']}")
    else:
        extra, secrets = telegram_endpoint(ctx)
        desired.update(extra)
        log.action(f"Configuring Telegram webhook target {name}")

    current = listing(WEBHOOK_ROOT).get(name)
    if current is None:
        pvesh("create", WEBHOOK_ROOT, "--name", name, *set_args(desired, []), *secret_args(secrets))
        log.ok(f"{name} created")
        return

    # Secrets persist across an update, so a target converted from Telegram to
    # Alertmanager would otherwise keep an unreferenced bot token on disk.
    unset = ENDPOINT_UNSET + (("secret",) if target == "alertmanager" else ())
    delete = stale_properties(current, unset)
    if not (secrets or delete or ctx.force_update or differs(current, desired)):
        log.sub(f"{name} unchanged")
        return

    pvesh("set", f"{WEBHOOK_ROOT}/{name}", *set_args(desired, delete), *secret_args(secrets))
    log.ok(f"{name} updated" + (f" (removed: {', '.join(delete)})" if delete else ""))


def configure_matcher(
    ctx: InstallContext, matchers: dict[str, dict], name: str, target_name: str
) -> None:
    desired: dict[str, object] = {
        "mode": "all",
        "target": [target_name],
        "comment": ctx.env["MATCHER_COMMENT"],
    }
    severities = list(names(ctx, "MATCH_SEVERITY"))
    unset = MATCHER_UNSET
    if severities:
        desired["match-severity"] = severities
    else:
        unset = unset + ("match-severity",)

    log.action(f"Configuring notification matcher {name}")
    current = matchers.get(name)
    if current is None:
        pvesh("create", MATCHER_ROOT, "--name", name, *set_args(desired, []))
        log.ok(f"{name} created")
        return

    # Update in place when the matcher already exists. The old delete-then-create
    # left alerting silently disabled whenever the create failed.
    delete = stale_properties(current, unset)
    if not (delete or ctx.force_update or differs(current, desired)):
        log.sub(f"{name} unchanged")
        return

    pvesh("set", f"{MATCHER_ROOT}/{name}", *set_args(desired, delete))
    log.ok(f"{name} updated" + (f" (removed: {', '.join(delete)})" if delete else ""))


def remove_stale(root: str, existing: dict[str, dict], stale: tuple[str, ...], keep: str) -> None:
    for name in stale:
        if name == keep or name not in existing:
            continue
        pvesh("delete", f"{root}/{name}")
        log.ok(f"Removed {name}")


def disable_builtin(root: str, existing: dict[str, dict], name: str) -> None:
    current = existing.get(name)
    if current is None:
        log.sub(f"{name} not present")
    elif str(current.get("disable", 0)) in ("1", "true"):
        log.sub(f"{name} already disabled")
    else:
        pvesh("set", f"{root}/{name}", "--disable", "1")
        log.ok(f"{name} disabled")


def install(ctx: InstallContext) -> None:
    log.header("PVE Notifications")
    # The staged bot token goes on every exit, as the bash's `trap ... EXIT` did --
    # including a refusal before the endpoint is ever touched.
    try:
        configure(ctx)
    finally:
        destroy_secret(ctx.build_dir / TELEGRAM_SECRET_NAME)


def configure(ctx: InstallContext) -> None:
    env.require(ctx, *REQUIRED_ENV)
    target = ctx.env["NOTIFY_TARGET"]
    if target not in TARGETS:
        raise InstallError(f"NOTIFY_TARGET must be one of {', '.join(TARGETS)}, got {target!r}")
    if not _which("pvesh"):
        raise InstallError("pvesh command not found")

    target_name = ctx.env["TARGET_NAME"]
    matcher_name = ctx.env["MATCHER_NAME"]
    disable_mail = env.flag(ctx, "DISABLE_MAIL_TO_ROOT", default=True)
    disable_default = env.flag(ctx, "DISABLE_DEFAULT_MATCHER", default=True)

    configure_endpoint(ctx, target, target_name)

    configure_matcher(ctx, listing(MATCHER_ROOT), matcher_name, target_name)

    log.action("Removing superseded matchers and targets")
    stale_matchers = names(ctx, "REMOVE_MATCHERS")
    stale_targets = names(ctx, "REMOVE_WEBHOOK_TARGETS")
    remove_stale(MATCHER_ROOT, listing(MATCHER_ROOT), stale_matchers, matcher_name)
    remove_stale(WEBHOOK_ROOT, listing(WEBHOOK_ROOT), stale_targets, target_name)

    # Only ever disabled, never re-enabled: `false` means "not managed here".
    if disable_mail:
        disable_builtin(SENDMAIL_ROOT, listing(SENDMAIL_ROOT), MAIL_TO_ROOT)
    if disable_default:
        disable_builtin(MATCHER_ROOT, listing(MATCHER_ROOT), DEFAULT_MATCHER)

    log.ok("PVE notifications configured")


if __name__ == "__main__":
    run(install, "PVE Notifications")
