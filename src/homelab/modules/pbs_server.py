"""pbs-server: converge a PBS server's own config (freender/homelab-ops#275).

Scope is deliberately narrower than "everything in /etc/proxmox-backup":

* **Written:** prune, verify and sync jobs; datastore GC/tuning/notification
  settings; remote host, auth-id and fingerprint; the Alertmanager webhook and its
  matcher; ACLs under `/datastore`.
* **Asserted, never written:** that each datastore exists at its `path`, that each
  remote exists (creating one needs its password, which this module never holds),
  and that every declared user and token exists (PBS mints token secrets itself).
* **Reported, never removed:** jobs, remotes, datastores, notification targets,
  users and `/datastore` ACLs the inventory does not declare. Removal is opt-in per
  ACL through `remove_acls`.

hosts.conf uses PBS's own property names (`keep-daily`, `remote-store`) so a value
can be checked against `proxmox-backup-debug api get` without translation. Each
kind's `owned` set is the API's own `--delete` list plus its non-deletable
required fields: a property the inventory stops declaring is deleted, not left
behind the way a partial `set` would.

`apply: false` (the default) makes a live deploy a read-only drift report.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import connection_for_host, normalize_bool, run_module_deploy
from ..output import print_sub

MODULE_DIR = "pbs-server"
REMOTE_ROOT = "/tmp/homelab-pbs-server"
PLAN_NAME = "plan.json"
ACL_SCOPE = "/datastore"

KEEP = ("keep-last", "keep-hourly", "keep-daily", "keep-weekly", "keep-monthly", "keep-yearly")


@dataclass(frozen=True)
class Kind:
    section: str
    label: str
    path: str
    key: str
    owned: tuple[str, ...]
    required: tuple[str, ...] = ()
    asserted: tuple[str, ...] = ()
    create: bool = True
    list_args: tuple[str, ...] = ()


DATASTORE = Kind(
    section="datastores",
    label="datastore",
    path="/config/datastore",
    key="name",
    # maintenance-mode is operational state, not config, and is never touched.
    owned=(
        "comment",
        "gc-schedule",
        "notification-mode",
        "tuning",
        "verify-new",
        "prune-schedule",
        *KEEP,
    ),
    required=("path",),
    asserted=("path",),
    create=False,
)
PRUNE = Kind(
    section="prune_jobs",
    label="prune job",
    path="/config/prune",
    key="id",
    owned=("store", "schedule", "comment", "disable", "ns", "max-depth", *KEEP),
    required=("store", "schedule"),
)
VERIFY = Kind(
    section="verify_jobs",
    label="verify job",
    path="/config/verify",
    key="id",
    owned=(
        "store",
        "schedule",
        "comment",
        "ignore-verified",
        "outdated-after",
        "ns",
        "max-depth",
        "read-threads",
        "verify-threads",
    ),
    required=("store", "schedule"),
)
SYNC = Kind(
    section="sync_jobs",
    label="sync job",
    path="/config/sync",
    key="id",
    owned=(
        "store",
        "remote",
        "remote-store",
        "sync-direction",
        "schedule",
        "comment",
        "ns",
        "remote-ns",
        "max-depth",
        "remove-vanished",
        "group-filter",
        "owner",
        "rate-in",
        "burst-in",
        "rate-out",
        "burst-out",
        "transfer-last",
        "encrypted-only",
        "verified-only",
        "resync-corrupt",
        "run-on-mount",
        "worker-threads",
    ),
    required=("store", "remote", "remote-store", "sync-direction", "schedule"),
    # A bare listing returns pull jobs only, which is how push jobs go unseen.
    list_args=("--sync-direction", "all"),
)
REMOTE = Kind(
    section="remotes",
    label="remote",
    path="/config/remote",
    key="name",
    owned=("host", "auth-id", "fingerprint", "comment", "port"),
    required=("host", "auth-id", "fingerprint"),
    create=False,
)
SECTION_KINDS = (DATASTORE, REMOTE, PRUNE, VERIFY, SYNC)

WEBHOOK = Kind(
    section="",
    label="webhook target",
    path="/config/notifications/endpoints/webhook",
    key="name",
    owned=("url", "method", "header", "body", "comment", "disable"),
)
MATCHER = Kind(
    section="",
    label="notification matcher",
    path="/config/notifications/matchers",
    key="name",
    owned=(
        "mode",
        "target",
        "match-severity",
        "match-field",
        "match-calendar",
        "invert-match",
        "disable",
        "comment",
    ),
)
# Built-ins are only ever disabled, so they own nothing beyond what they declare.
SENDMAIL = Kind(
    section="",
    label="sendmail target",
    path="/config/notifications/endpoints/sendmail",
    key="name",
    owned=(),
    create=False,
)
BUILTIN_MATCHER = Kind(
    section="",
    label="notification matcher",
    path=MATCHER.path,
    key="name",
    owned=(),
    create=False,
)

WEBHOOK_NAME = "Alertmanager"
MATCHER_NAME = "alertmanager-matcher"
NOTIFY_COMMENT = "Route notifications to Alertmanager"


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
    install_script = root / MODULE_DIR / "scripts" / "install.py"
    if not install_script.is_file():
        raise ValueError(f"missing install script: {install_script}")
    for host in hosts:
        build_plan(root, host)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    plan = build_plan(root, host)
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)
    (build_dir / PLAN_NAME).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    mode = "apply" if plan["apply"] else "report drift (apply: false)"
    if dry_run:
        print_sub(f"[DRY-RUN] Would {mode} on {host}:{REMOTE_ROOT}/")
        print_sub(f"Managed objects: {len(plan['objects'])}, ACLs: {len(plan['acls']['grants'])}")
        return

    print_sub(f"Mode: {mode}")
    stage_and_run_remote_installer(
        root,
        connection_for_host(root, host),
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.py",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "src", "scripts"),
    )


def build_plan(root: Path, host: str) -> dict[str, object]:
    registry = default_registry(root)
    if str(registry.get(host, "config.type", "")) != "pbs":
        raise ValueError(f"{MODULE_DIR} supports PBS hosts only: {host}")
    config = registry.get(host, MODULE_DIR, {}) or {}
    if not isinstance(config, dict):
        raise ValueError(f"{MODULE_DIR} must be a mapping for {host}")

    sections = {kind.section: section_items(config, kind, host) for kind in SECTION_KINDS}
    if not sections[DATASTORE.section]:
        raise ValueError(f"{MODULE_DIR}.datastores must declare at least one datastore for {host}")
    check_references(sections, host)
    check_push_jobs(sections[SYNC.section], host)

    users = name_list(config.get("users"), f"{MODULE_DIR}.users", host)
    objects = [
        plan_object(kind, name, props)
        for kind in SECTION_KINDS
        for name, props in sections[kind.section].items()
    ]
    objects += notification_objects(config.get("notifications"), host)
    return {
        "apply": normalize_bool(
            config.get("apply"), False, f"{MODULE_DIR}.apply must be boolean for {host}"
        ),
        "objects": objects,
        "unmanaged": unmanaged_scopes(objects),
        "acls": acl_plan(config, users, host),
        "users": users,
    }


def section_items(config: dict, kind: Kind, host: str) -> dict[str, dict]:
    key = f"{MODULE_DIR}.{kind.section}"
    raw = config.get(kind.section) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{key} must be a mapping of name -> properties for {host}")
    return {
        check_name(name, key, host): check_props(props, kind, f"{key}.{name}", host)
        for name, props in raw.items()
    }


def check_name(name: object, key: str, host: str) -> str:
    text = str(name)
    if not text or any(char.isspace() or char == "/" for char in text):
        raise ValueError(f"{key}: invalid name {text!r} for {host}")
    return text


def check_props(props: object, kind: Kind, key: str, host: str) -> dict:
    if not isinstance(props, dict):
        raise ValueError(f"{key} must be a mapping for {host}")
    allowed = set(kind.owned) | set(kind.asserted)
    unknown = sorted(set(props) - allowed)
    if unknown:
        raise ValueError(f"{key}: unknown {kind.label} properties {unknown} for {host}")
    missing = [prop for prop in kind.required if props.get(prop) in (None, "")]
    if missing:
        raise ValueError(f"{key}: missing required {missing} for {host}")
    return dict(props)


def check_references(sections: dict[str, dict], host: str) -> None:
    datastores = set(sections[DATASTORE.section])
    remotes = set(sections[REMOTE.section])
    for kind in (PRUNE, VERIFY, SYNC):
        for name, props in sections[kind.section].items():
            if props["store"] not in datastores:
                raise ValueError(
                    f"{kind.label} {name}: undeclared datastore {props['store']!r} for {host}"
                )
    for name, props in sections[SYNC.section].items():
        if props["remote"] not in remotes:
            raise ValueError(f"sync job {name}: undeclared remote {props['remote']!r} for {host}")


# Settings every push job must agree on, so DR copies cannot quietly diverge in
# what they carry.
PUSH_UNIFORM = ("max-depth", "encrypted-only")


def check_push_jobs(jobs: dict[str, dict], host: str) -> None:
    push = {name: props for name, props in jobs.items() if props["sync-direction"] == "push"}
    for name, props in push.items():
        # Offsite stays add-only: a push with remove-vanished deletes DR history
        # whenever xur prunes (homelab-ops#263/#265).
        if props.get("remove-vanished") not in (None, False):
            raise ValueError(f"sync job {name}: push jobs must not set remove-vanished for {host}")
    for prop in PUSH_UNIFORM:
        values = {name: props.get(prop) for name, props in push.items()}
        if len(set(map(repr, values.values()))) > 1:
            raise ValueError(f"push jobs disagree on {prop} for {host}: {values}")


def plan_object(kind: Kind, name: str, props: dict) -> dict[str, object]:
    return {
        "label": f"{kind.label} {name}",
        "path": kind.path,
        "key": kind.key,
        "id": name,
        "props": {prop: value for prop, value in props.items() if prop not in kind.asserted},
        "asserted": {prop: props[prop] for prop in kind.asserted},
        "owned": list(kind.owned),
        "create": kind.create,
        "list_args": list(kind.list_args),
    }


def b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def alertmanager_body() -> str:
    """PBS events as Alertmanager alerts, the same shape `pve-notifications` posts
    for PVE with `source: pbs` and PBS's own job fields in place of `vmid`."""
    body = [
        {
            "labels": {
                "alertname": "ProxmoxNotification",
                "severity": "critical",
                "source": "pbs",
                "host": "{{ fields.hostname }}",
                "name": "{{ fields.type }}",
                "datastore": "{{ fields.datastore }}",
                "jobid": "{{ fields.[job-id] }}",
                "pve_severity": "{{ severity }}",
            },
            "annotations": {
                "summary": "{{ escape title }}",
                "description": "{{ escape message }}",
            },
        }
    ]
    return b64(json.dumps(body, separators=(",", ":")))


def notification_objects(config: object, host: str) -> list[dict[str, object]]:
    if config in (None, {}):
        return []
    if not isinstance(config, dict):
        raise ValueError(f"{MODULE_DIR}.notifications must be a mapping for {host}")
    url = str(config.get("alertmanager_url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError(
            f"{MODULE_DIR}.notifications.alertmanager_url must be an http(s) URL for {host}"
        )
    severity = name_list(
        config.get("match_severity", ["error"]), f"{MODULE_DIR}.notifications.match_severity", host
    )
    webhook = {
        "url": f"{url.rstrip('/')}/api/v2/alerts",
        "method": "post",
        "header": [f"name=Content-Type,value={b64('application/json')}"],
        "body": alertmanager_body(),
        "comment": NOTIFY_COMMENT,
    }
    matcher = {
        "mode": "all",
        "target": [WEBHOOK_NAME],
        "match-severity": severity,
        "comment": NOTIFY_COMMENT,
    }
    return [
        plan_object(WEBHOOK, WEBHOOK_NAME, webhook),
        plan_object(MATCHER, MATCHER_NAME, matcher),
        plan_object(SENDMAIL, "mail-to-root", {"disable": True}),
        plan_object(BUILTIN_MATCHER, "default-matcher", {"disable": True}),
    ]


def unmanaged_scopes(objects: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collections whose undeclared entries are reported, with the names declared."""
    scopes: dict[str, dict[str, object]] = {}
    for obj in objects:
        scope = scopes.setdefault(
            obj["path"],
            {"path": obj["path"], "key": obj["key"], "list_args": obj["list_args"], "known": []},
        )
        scope["known"].append(obj["id"])
    return list(scopes.values())


def name_list(value: object, key: str, host: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list for {host}")
    return [check_name(item, key, host) for item in value]


def acl_plan(config: dict, users: list[str], host: str) -> dict[str, object]:
    grants = acl_entries(config.get("acls"), f"{MODULE_DIR}.acls", host)
    remove = acl_entries(config.get("remove_acls"), f"{MODULE_DIR}.remove_acls", host)
    unknown = sorted({grant["auth-id"] for grant in grants} - set(users))
    if unknown:
        raise ValueError(f"{MODULE_DIR}.acls grant to undeclared users {unknown} for {host}")
    overlap = [entry for entry in remove if entry in grants]
    if overlap:
        raise ValueError(f"{MODULE_DIR}.remove_acls overlaps acls for {host}: {overlap}")
    return {"scope": ACL_SCOPE, "grants": grants, "remove": remove}


def acl_entries(value: object, key: str, host: str) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list for {host}")
    return [
        entry
        for index, item in enumerate(value)
        for entry in acl_entry(item, f"{key}[{index}]", host)
    ]


def acl_entry(item: object, key: str, host: str) -> list[dict[str, str]]:
    if not isinstance(item, dict) or set(item) != {"path", "auth-id", "roles"}:
        raise ValueError(f"{key} must be a mapping of path, auth-id, roles for {host}")
    path = str(item["path"])
    if path != ACL_SCOPE and not path.startswith(f"{ACL_SCOPE}/"):
        raise ValueError(f"{key}: only {ACL_SCOPE} ACLs are managed, got {path!r} for {host}")
    roles = name_list(item["roles"], f"{key}.roles", host)
    if not roles:
        raise ValueError(f"{key}.roles must not be empty for {host}")
    auth_id = check_name(item["auth-id"], f"{key}.auth-id", host)
    return [{"path": path, "auth-id": auth_id, "role": role} for role in roles]
