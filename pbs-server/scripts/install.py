#!/usr/bin/env python3
"""Remote installer for the pbs-server module (freender/homelab-ops#275).

A generic converger: `build/<host>/plan.json` lists each managed object with its
API collection, the properties it should have, the properties it owns, and the
ones that are only asserted. Every read and write goes through
`proxmox-backup-debug api`, the pvesh-style client for the local PBS API, so the
module never edits `/etc/proxmox-backup` behind the daemon's locks.

* **A converged object writes nothing.** Values are compared after
  normalisation, so the webhook body is compared as decoded JSON and a remote
  fingerprint case-insensitively; neither is rewritten for formatting alone.
* **`set` takes away as well as adds.** An owned property the plan no longer
  declares is passed as `--delete`, so a hand-set `max-depth` or
  `remove-vanished` does not survive a deploy.
* **Asserted properties are never written.** A datastore whose `path` differs
  fails the deploy; nothing here can move a datastore.
* **Report mode is the default.** With `apply` false nothing is written; the run
  lists what would change and fails only on errors an apply could not fix.
* **An applied change is read back.** Anything still drifting after the writes
  fails the deploy instead of reporting success.
* **A container-hosted PBS is reached through `docker exec`.** The plan's
  `container` prefixes every call; the orchestrator keeps such hosts report-only.
* **`add_only` accounts are checked on every ACL path.** A role outside the
  allowed set, on any path, is an error rather than a warning.
"""

from __future__ import annotations

import base64
import binascii
import json
import shutil
import subprocess

from homelab_install import log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which

PLAN_NAME = "plan.json"
CLIENT = "proxmox-backup-debug"
ACL_PATH = "/access/acl"
USERS_PATH = "/access/users"

# `docker exec <container>` for a PBS in Docker; set once per run by `install`.
_prefix: list[str] = []


def api(command: str, path: str, *args: str) -> str:
    cmd = [*_prefix, CLIENT, "api", command, path, *args]
    if command == "get":
        cmd += ["--output-format", "json"]
    result = _run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"{CLIENT} api {command} {path} failed: {detail}")
    return result.stdout


def api_get(path: str, *args: str) -> object:
    return json.loads(api("get", path, *args) or "null")


def listing(path: str, key: str, list_args: list[str]) -> dict[str, dict]:
    return {entry[key]: entry for entry in api_get(path, *list_args) or []}


def decoded_json(value: str) -> object:
    try:
        return json.loads(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        return value


def canon(key: str, value: object) -> object:
    """A comparable form of one property. Empty is absent: PBS reports the root
    namespace as `ns: ""`, which is the same as no `ns` at all."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return tuple(canon(key, item) for item in value)
    if key == "fingerprint":
        return str(value).lower()
    if key == "body":
        return json.dumps(decoded_json(str(value)), sort_keys=True)
    return str(value)


def drift(obj: dict, live: dict) -> tuple[dict, list[str]]:
    changes = {
        key: value
        for key, value in obj["props"].items()
        if canon(key, live.get(key)) != canon(key, value)
    }
    deletes = [
        key
        for key in obj["owned"]
        if key not in obj["props"] and canon(key, live.get(key)) is not None
    ]
    return changes, deletes


def assertion_errors(obj: dict, live: dict) -> list[str]:
    return [
        f"{obj['label']}: {key} is {live.get(key)!r}, inventory says {value!r} (never written here)"
        for key, value in obj["asserted"].items()
        if canon(key, live.get(key)) != canon(key, value)
    ]


def render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def set_args(props: dict, deletes: list[str]) -> list[str]:
    args: list[str] = []
    for key, value in props.items():
        for item in value if isinstance(value, list) else [value]:
            args += [f"--{key}", render_value(item)]
    for key in deletes:
        args += ["--delete", key]
    return args


def describe(changes: dict, deletes: list[str], live: dict) -> list[str]:
    lines = [f"{key}: {live.get(key)!r} -> {value!r}" for key, value in changes.items()]
    return lines + [f"{key}: {live.get(key)!r} -> (deleted)" for key in deletes]


class Run:
    """One pass over the plan: counts drift, collects errors, writes when applying."""

    def __init__(self, plan: dict) -> None:
        self.apply = bool(plan.get("apply"))
        self.pending = 0
        self.errors: list[str] = []
        self._listings: dict[str, dict[str, dict]] = {}

    def collection(self, obj: dict) -> dict[str, dict]:
        path = obj["path"]
        if path not in self._listings:
            self._listings[path] = listing(path, obj["key"], obj["list_args"])
        return self._listings[path]

    def write(self, message: str, command: str, path: str, args: list[str]) -> None:
        self.pending += 1
        if not self.apply:
            log.warn(f"would {message}")
            return
        api(command, path, *args)
        log.ok(message)

    def converge(self, obj: dict) -> None:
        live = self.collection(obj).get(obj["id"])
        if live is None:
            self.converge_missing(obj)
            return
        errors = assertion_errors(obj, live)
        if errors:
            self.errors += errors
            return
        changes, deletes = drift(obj, live)
        if not (changes or deletes):
            log.sub(f"{obj['label']} unchanged")
            return
        for line in describe(changes, deletes, live):
            log.sub(f"{obj['label']}: {line}")
        self.write(
            f"update {obj['label']}",
            "set",
            f"{obj['path']}/{obj['id']}",
            set_args(changes, deletes),
        )

    def converge_missing(self, obj: dict) -> None:
        if not obj["create"]:
            self.errors.append(f"{obj['label']} does not exist and is never created here")
            return
        args = [f"--{obj['key']}", obj["id"], *set_args(obj["props"], [])]
        self.write(f"create {obj['label']}", "create", obj["path"], args)


def report_unmanaged(scopes: list[dict]) -> None:
    for scope in scopes:
        live = listing(scope["path"], scope["key"], scope["list_args"])
        for name in sorted(set(live) - set(scope["known"])):
            log.warn(f"unmanaged {scope['path']}/{name} (reported, not removed)")


def acl_key(entry: dict) -> tuple[str, str, str]:
    return (entry["path"], entry["auth-id"], entry["role"])


def live_acls(scope: str) -> dict[tuple[str, str, str], dict]:
    entries = api_get(ACL_PATH) or []
    return {
        (entry["path"], entry["ugid"], entry["roleid"]): entry
        for entry in entries
        if entry.get("ugid_type") == "user"
        and (entry["path"] == scope or entry["path"].startswith(f"{scope}/"))
    }


def acl_args(key: tuple[str, str, str], delete: bool) -> list[str]:
    path, auth_id, role = key
    args = ["--path", path, "--role", role, "--auth-id", auth_id]
    return args + (["--delete", "true"] if delete else ["--propagate", "true"])


def converge_acls(state: Run, acls: dict) -> None:
    live = live_acls(acls["scope"])
    grants = {acl_key(entry) for entry in acls["grants"]}
    remove = {acl_key(entry) for entry in acls["remove"]}
    for key in sorted(grants):
        entry = live.get(key)
        if entry is not None and entry.get("propagate", True):
            log.sub(f"ACL {' '.join(key)} present")
            continue
        state.write(f"grant ACL {' '.join(key)}", "set", ACL_PATH, acl_args(key, delete=False))
    for key in sorted(set(live) - grants):
        if key in remove:
            state.write(f"remove ACL {' '.join(key)}", "set", ACL_PATH, acl_args(key, delete=True))
        else:
            log.warn(f"unmanaged ACL {' '.join(key)} (reported, not removed)")


def check_users(state: Run, expected: list[str]) -> None:
    present: set[str] = set()
    for user in api_get(USERS_PATH, "--include_tokens", "true") or []:
        present.add(user["userid"])
        present.update(token["tokenid"] for token in user.get("tokens", []))
    for missing in sorted(set(expected) - present):
        state.errors.append(f"user {missing} does not exist and is never created here")
    for extra in sorted(present - set(expected) - {"root@pam"}):
        log.warn(f"unmanaged user {extra} (reported, not removed)")


def check_add_only(state: Run, add_only: dict) -> None:
    accounts = add_only.get("auth_ids", [])
    if not accounts:
        return
    allowed = set(add_only.get("roles", []))
    # The account's tokens (`xur-sync@pbs!t`) are held to the same limit.
    violations = [
        entry
        for entry in api_get(ACL_PATH) or []
        if entry["ugid"].split("!", 1)[0] in accounts and entry["roleid"] not in allowed
    ]
    for entry in violations:
        state.errors.append(
            f"add-only account {entry['ugid']} holds {entry['roleid']} on {entry['path']}; "
            f"only {sorted(allowed)} are allowed"
        )
    if not violations:
        log.sub(f"add-only {', '.join(accounts)}: no role beyond {sorted(allowed)} on any path")


def converge(plan: dict) -> Run:
    state = Run(plan)
    for obj in plan["objects"]:
        state.converge(obj)
    converge_acls(state, plan["acls"])
    check_users(state, plan["users"])
    check_add_only(state, plan.get("add_only", {}))
    return state


def verify_converged(plan: dict) -> None:
    """Re-read everything after applying; a write that did not land fails the deploy."""
    recheck = converge({**plan, "apply": False})
    if recheck.pending or recheck.errors:
        raise InstallError(f"{recheck.pending} change(s) still pending after apply; host diverged")


def load_plan(ctx: InstallContext) -> dict:
    path = ctx.build_dir / PLAN_NAME
    if not path.is_file():
        raise InstallError(f"missing plan: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def install(ctx: InstallContext) -> None:
    global _prefix
    log.header("PBS Server Config")
    plan = load_plan(ctx)
    container = plan.get("container") or ""
    if container:
        if not _which("docker"):
            raise InstallError(f"docker not found; cannot reach PBS container {container}")
        _prefix = ["docker", "exec", container]
    else:
        if not _which(CLIENT):
            raise InstallError(f"{CLIENT} not found; is this a PBS host?")
        _prefix = []

    # Always a read-only pass first: an error anywhere means nothing is written.
    log.action("Checking PBS config drift")
    report = converge({**plan, "apply": False})
    report_unmanaged(plan["unmanaged"])
    if report.errors:
        for error in report.errors:
            log.error(error)
        raise InstallError(f"{len(report.errors)} error(s) the module cannot fix; nothing written")

    if not report.pending:
        log.ok("PBS config converged")
    elif not plan.get("apply"):
        log.warn(f"{report.pending} change(s) pending; set pbs-server.apply: true to converge")
    else:
        log.action("Applying PBS config")
        converge(plan)
        verify_converged(plan)
        log.ok(f"{report.pending} change(s) applied and verified")


if __name__ == "__main__":
    run(install, "PBS Server Config")
