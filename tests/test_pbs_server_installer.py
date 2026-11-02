"""pbs-server: remote installer (freender/homelab-ops#275).

`proxmox-backup-debug api` is replaced by an in-memory model of the PBS API that
applies create / set / --delete the way PBS does, so each test asserts on the
resulting config rather than on which commands happened to run.

What these pin:

* **Report mode writes nothing**, and neither does a converged apply.
* **`set` takes away as well as adds** -- an owned property the plan dropped goes.
* **An error anywhere means no write anywhere**, including an asserted datastore
  path, a remote it may not create, or a missing user.
* **ACLs outside `/datastore` are invisible**, unmanaged ones are only reported,
  and removal happens only for entries listed in `remove`.
* **A write that did not land fails the deploy.**
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "pbs-server" / "scripts" / "install.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("pbs_server_installer", INSTALLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_installer()

COLLECTION_KEYS = {
    "/config/datastore": "name",
    "/config/sync": "id",
    "/config/remote": "name",
    "/config/notifications/endpoints/webhook": "name",
}


def parse_args(args: list[str]) -> tuple[dict[str, object], list[str]]:
    props: dict[str, object] = {}
    deletes: list[str] = []
    it = iter(args)
    for flag in it:
        key, value = flag.removeprefix("--"), next(it)
        if key == "delete":
            deletes.append(value)
        elif key in props:
            props[key] = [*(props[key] if isinstance(props[key], list) else [props[key]]), value]
        else:
            props[key] = value
    return props, deletes


class FakePbs:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {path: {} for path in COLLECTION_KEYS}
        self.acls: list[dict] = []
        self.users: list[dict] = []
        self.writes: list[list[str]] = []
        self.ignore_writes = False

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        command, path, args = cmd[2], cmd[3], cmd[4:]
        if command == "get":
            return self.result(json.dumps(self.get(path)))
        self.writes.append(cmd)
        if not self.ignore_writes:
            self.mutate(command, path, args)
        return self.result("")

    @staticmethod
    def result(stdout: str, code: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], code, stdout=stdout, stderr="boom" if code else "")

    def get(self, path: str) -> object:
        if path == installer.ACL_PATH:
            return self.acls
        if path == installer.USERS_PATH:
            return self.users
        return list(self.collections[path].values())

    def mutate(self, command: str, path: str, args: list[str]) -> None:
        props, deletes = parse_args(args)
        if path == installer.ACL_PATH:
            # On /access/acl `--delete` is a boolean, not a property list.
            self.set_acl(props, delete=deletes == ["true"])
            return
        if command == "create":
            key = COLLECTION_KEYS[path]
            self.collections[path][props[key]] = props
            return
        collection, _, name = path.rpartition("/")
        entry = self.collections[collection][name]
        entry.update(props)
        for key in deletes:
            entry.pop(key, None)

    def set_acl(self, props: dict, delete: bool) -> None:
        key = (props["path"], props["auth-id"], props["role"])
        self.acls = [a for a in self.acls if (a["path"], a["ugid"], a["roleid"]) != key]
        if not delete:
            self.acls.append(
                {
                    "path": key[0],
                    "ugid": key[1],
                    "roleid": key[2],
                    "ugid_type": "user",
                    "propagate": True,
                }
            )


def acl(path: str, ugid: str, role: str, **extra: object) -> dict:
    return {
        "path": path,
        "ugid": ugid,
        "roleid": role,
        "ugid_type": "user",
        "propagate": True,
        **extra,
    }


BODY = base64.b64encode(
    json.dumps([{"labels": {"a": "b"}}], separators=(",", ":")).encode()
).decode()


def obj(
    label: str, path: str, key: str, ident: str, props: dict, owned: list[str], **extra: object
) -> dict:
    return {
        "label": label,
        "path": path,
        "key": key,
        "id": ident,
        "props": props,
        "asserted": extra.get("asserted", {}),
        "owned": owned,
        "create": extra.get("create", True),
        "list_args": extra.get("list_args", []),
    }


def make_plan(**changes: object) -> dict:
    plan = {
        "apply": False,
        "objects": [
            obj(
                "datastore backup",
                "/config/datastore",
                "name",
                "backup",
                {"gc-schedule": "daily"},
                ["gc-schedule", "keep-daily"],
                asserted={"path": "/backup"},
                create=False,
            ),
            obj(
                "remote zavala-push",
                "/config/remote",
                "name",
                "zavala-push",
                {"host": "cinci.internal", "fingerprint": "aa:bb"},
                ["host", "fingerprint", "port"],
                create=False,
            ),
            obj(
                "sync job push",
                "/config/sync",
                "id",
                "push",
                {"store": "backup", "schedule": "00:00", "ns": "freender"},
                ["store", "schedule", "ns", "max-depth", "remove-vanished"],
                list_args=["--sync-direction", "all"],
            ),
            obj(
                "webhook target Alertmanager",
                "/config/notifications/endpoints/webhook",
                "name",
                "Alertmanager",
                {"body": BODY, "header": ["h1"]},
                ["body", "header", "disable"],
            ),
        ],
        "unmanaged": [{"path": "/config/sync", "key": "id", "list_args": [], "known": ["push"]}],
        "acls": {
            "scope": "/datastore",
            "grants": [
                {
                    "path": "/datastore/backup",
                    "auth-id": "pve-backup@pbs",
                    "role": "DatastoreBackup",
                }
            ],
            "remove": [],
        },
        "users": ["pve-backup@pbs", "cinci-backup@pbs!host-backup"],
    }
    plan.update(changes)
    return plan


def converged_pbs() -> FakePbs:
    pbs = FakePbs()
    pbs.collections["/config/datastore"]["backup"] = {
        "name": "backup",
        "path": "/backup",
        "gc-schedule": "daily",
    }
    pbs.collections["/config/remote"]["zavala-push"] = {
        "name": "zavala-push",
        "host": "cinci.internal",
        "fingerprint": "AA:BB",
        "auth-id": "xur-sync@pbs",
    }
    pbs.collections["/config/sync"]["push"] = {
        "id": "push",
        "store": "backup",
        "schedule": "00:00",
        "ns": "freender",
        "sync-direction": "push",
    }
    # Live body carries a trailing newline the plan does not.
    pbs.collections["/config/notifications/endpoints/webhook"]["Alertmanager"] = {
        "name": "Alertmanager",
        "body": base64.b64encode(base64.b64decode(BODY) + b"\n").decode(),
        "header": ["h1"],
        "origin": "user-created",
    }
    pbs.acls = [acl("/datastore/backup", "pve-backup@pbs", "DatastoreBackup")]
    pbs.users = [
        {"userid": "root@pam"},
        {"userid": "pve-backup@pbs"},
        {"userid": "cinci-backup@pbs", "tokens": [{"tokenid": "cinci-backup@pbs!host-backup"}]},
    ]
    return pbs


@pytest.fixture
def pbs(monkeypatch: pytest.MonkeyPatch) -> FakePbs:
    fake = converged_pbs()
    monkeypatch.setattr(installer, "_run", fake)
    monkeypatch.setattr(installer, "_which", lambda name: f"/usr/bin/{name}")
    return fake


def ctx_for(tmp_path: Path, plan: dict) -> InstallContext:
    build_dir = tmp_path / "build" / "xur"
    build_dir.mkdir(parents=True)
    (build_dir / installer.PLAN_NAME).write_text(json.dumps(plan), encoding="utf-8")
    return InstallContext(
        host="xur",
        script_dir=tmp_path,
        build_dir=build_dir,
        env={},
        deploy_env={},
        file_map={},
        force_update=False,
    )


def run(tmp_path: Path, plan: dict) -> None:
    installer.install(ctx_for(tmp_path, plan))


def test_converged_config_writes_nothing(pbs: FakePbs, tmp_path: Path) -> None:
    run(tmp_path, make_plan(apply=True))
    assert pbs.writes == []


def test_report_mode_writes_nothing_but_counts_drift(pbs: FakePbs, tmp_path: Path, capsys) -> None:
    pbs.collections["/config/sync"]["push"]["max-depth"] = 0

    run(tmp_path, make_plan())

    assert pbs.writes == []
    out = capsys.readouterr().out
    assert "max-depth: 0 -> (deleted)" in out
    assert "1 change(s) pending" in out


def test_apply_deletes_owned_property_the_plan_dropped(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.collections["/config/sync"]["push"]["max-depth"] = 0
    pbs.collections["/config/sync"]["push"]["remove-vanished"] = True

    run(tmp_path, make_plan(apply=True))

    live = pbs.collections["/config/sync"]["push"]
    assert "max-depth" not in live and "remove-vanished" not in live
    assert live["sync-direction"] == "push"  # not owned by this plan, so untouched
    assert pbs.writes == [
        [
            "proxmox-backup-debug",
            "api",
            "set",
            "/config/sync/push",
            "--delete",
            "max-depth",
            "--delete",
            "remove-vanished",
        ]
    ]


def test_apply_sets_only_changed_properties(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.collections["/config/sync"]["push"]["schedule"] = "01:00"

    run(tmp_path, make_plan(apply=True))

    assert pbs.collections["/config/sync"]["push"]["schedule"] == "00:00"
    assert pbs.writes[0][4:] == ["--schedule", "00:00"]


def test_empty_live_namespace_is_absent(pbs: FakePbs, tmp_path: Path) -> None:
    plan = make_plan(apply=True)
    del plan["objects"][2]["props"]["ns"]
    pbs.collections["/config/sync"]["push"]["ns"] = ""

    run(tmp_path, plan)

    assert pbs.writes == []


def test_missing_job_is_created(pbs: FakePbs, tmp_path: Path) -> None:
    del pbs.collections["/config/sync"]["push"]

    run(tmp_path, make_plan(apply=True))

    assert pbs.collections["/config/sync"]["push"]["schedule"] == "00:00"
    assert pbs.writes[0][2:6] == ["create", "/config/sync", "--id", "push"]


def test_missing_remote_is_an_error_and_blocks_every_write(pbs: FakePbs, tmp_path: Path) -> None:
    del pbs.collections["/config/remote"]["zavala-push"]
    pbs.collections["/config/sync"]["push"]["schedule"] = "01:00"

    with pytest.raises(InstallError, match="nothing written"):
        run(tmp_path, make_plan(apply=True))
    assert pbs.writes == []


def test_asserted_datastore_path_is_never_written(pbs: FakePbs, tmp_path: Path, capsys) -> None:
    pbs.collections["/config/datastore"]["backup"]["path"] = "/elsewhere"

    with pytest.raises(InstallError):
        run(tmp_path, make_plan(apply=True))
    assert pbs.writes == []
    assert "never written here" in capsys.readouterr().err


def test_missing_user_is_an_error(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.users = [{"userid": "pve-backup@pbs"}]

    with pytest.raises(InstallError):
        run(tmp_path, make_plan())


def test_extra_user_is_reported_not_removed(pbs: FakePbs, tmp_path: Path, capsys) -> None:
    pbs.users.append({"userid": "admin@pbs", "enable": True})

    run(tmp_path, make_plan(apply=True))

    assert "unmanaged user admin@pbs" in capsys.readouterr().out
    assert pbs.writes == []


def test_fingerprint_compares_case_insensitively(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.collections["/config/remote"]["zavala-push"]["fingerprint"] = "aa:cc"

    run(tmp_path, make_plan(apply=True))

    assert pbs.collections["/config/remote"]["zavala-push"]["fingerprint"] == "aa:bb"


def test_unmanaged_job_is_reported_not_removed(pbs: FakePbs, tmp_path: Path, capsys) -> None:
    pbs.collections["/config/sync"]["hand-made"] = {"id": "hand-made", "store": "backup"}

    run(tmp_path, make_plan(apply=True))

    assert "unmanaged /config/sync/hand-made" in capsys.readouterr().out
    assert "hand-made" in pbs.collections["/config/sync"]


def test_missing_acl_is_granted(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.acls = []

    run(tmp_path, make_plan(apply=True))

    assert pbs.acls == [acl("/datastore/backup", "pve-backup@pbs", "DatastoreBackup")]


def test_non_propagating_acl_is_regranted(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.acls = [acl("/datastore/backup", "pve-backup@pbs", "DatastoreBackup", propagate=False)]

    run(tmp_path, make_plan(apply=True))

    assert pbs.acls[0]["propagate"] is True


def test_unmanaged_acl_is_reported_and_out_of_scope_ignored(
    pbs: FakePbs, tmp_path: Path, capsys
) -> None:
    stale = acl("/datastore/prepared-lxcs", "pve-backup@pbs", "DatastoreReader")
    admin = acl("/", "root@pam!pdm-rasputin", "Admin")
    pbs.acls += [stale, admin]

    run(tmp_path, make_plan(apply=True))

    out = capsys.readouterr().out
    assert "unmanaged ACL /datastore/prepared-lxcs" in out
    assert "pdm-rasputin" not in out
    assert stale in pbs.acls and admin in pbs.acls


def test_listed_acl_is_removed(pbs: FakePbs, tmp_path: Path) -> None:
    stale = acl("/datastore/prepared-lxcs", "pve-backup@pbs", "DatastoreReader")
    pbs.acls.append(stale)
    plan = make_plan(apply=True)
    plan["acls"]["remove"] = [
        {"path": "/datastore/prepared-lxcs", "auth-id": "pve-backup@pbs", "role": "DatastoreReader"}
    ]

    run(tmp_path, plan)

    assert stale not in pbs.acls


def test_write_that_did_not_land_fails(pbs: FakePbs, tmp_path: Path) -> None:
    pbs.collections["/config/sync"]["push"]["schedule"] = "01:00"
    pbs.ignore_writes = True

    with pytest.raises(InstallError, match="diverged"):
        run(tmp_path, make_plan(apply=True))


def test_api_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "_run", lambda cmd, **_: FakePbs.result("", 1))
    with pytest.raises(InstallError, match="boom"):
        installer.api_get("/config/sync")


def test_refuses_without_pbs_client(
    pbs: FakePbs, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(installer, "_which", lambda name: None)
    with pytest.raises(InstallError, match="PBS host"):
        run(tmp_path, make_plan())


def test_list_values_render_as_repeated_options() -> None:
    assert installer.set_args({"target": ["a", "b"], "disable": True}, ["comment"]) == [
        "--target",
        "a",
        "--target",
        "b",
        "--disable",
        "true",
        "--delete",
        "comment",
    ]


def test_canon_treats_bool_and_body_forms() -> None:
    assert installer.canon("disable", True) == installer.canon("disable", "true")
    assert installer.canon("comment", []) is None
    assert installer.canon("body", "not-base64!") == json.dumps("not-base64!")


def test_plan_is_not_mutated_by_a_run(pbs: FakePbs, tmp_path: Path) -> None:
    plan = make_plan(apply=True)
    before = copy.deepcopy(plan)
    installer.converge(plan)
    assert plan == before
