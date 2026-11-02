"""pbs-server: orchestrator plan and inventory invariants (freender/homelab-ops#275)."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from homelab.modules import pbs_server

REPO_ROOT = Path(__file__).resolve().parents[1]

# The webhook body xur carries today, hand-written with a trailing newline. The
# plan must decode to the same JSON or the first apply rewrites the alert shape.
LIVE_BODY = (
    "W3sibGFiZWxzIjp7ImFsZXJ0bmFtZSI6IlByb3htb3hOb3RpZmljYXRpb24iLCJzZXZlcml0eSI6ImNyaXRpY2Fs"
    "Iiwic291cmNlIjoicGJzIiwiaG9zdCI6Int7IGZpZWxkcy5ob3N0bmFtZSB9fSIsIm5hbWUiOiJ7eyBmaWVsZHMu"
    "dHlwZSB9fSIsImRhdGFzdG9yZSI6Int7IGZpZWxkcy5kYXRhc3RvcmUgfX0iLCJqb2JpZCI6Int7IGZpZWxkcy5b"
    "am9iLWlkXSB9fSIsInB2ZV9zZXZlcml0eSI6Int7IHNldmVyaXR5IH19In0sImFubm90YXRpb25zIjp7InN1bW1h"
    "cnkiOiJ7eyBlc2NhcGUgdGl0bGUgfX0iLCJkZXNjcmlwdGlvbiI6Int7IGVzY2FwZSBtZXNzYWdlIH19In19XQo="
)

BASE = {
    "datastores": {"backup": {"path": "/backup", "gc-schedule": "daily"}},
    "remotes": {
        "zavala-push": {
            "host": "cinci.internal",
            "auth-id": "xur-sync@pbs",
            "fingerprint": "aa:bb",
        },
        "cottonwood-push": {
            "host": "cottonwood.internal",
            "auth-id": "xur-sync@pbs",
            "fingerprint": "cc:dd",
        },
    },
    "prune_jobs": {"p": {"store": "backup", "schedule": "daily", "keep-daily": 7}},
    "sync_jobs": {
        "push-a": {
            "sync-direction": "push",
            "store": "backup",
            "remote": "zavala-push",
            "remote-store": "backup",
            "schedule": "00:00",
        },
        "push-b": {
            "sync-direction": "push",
            "store": "backup",
            "remote": "cottonwood-push",
            "remote-store": "backup",
            "schedule": "00:30",
        },
    },
    "users": ["pve-backup@pbs"],
    "acls": [
        {"path": "/datastore/backup", "auth-id": "pve-backup@pbs", "roles": ["DatastoreBackup"]}
    ],
}


class FakeRegistry:
    def __init__(self, config: dict, host_type: str = "pbs") -> None:
        self._values = {"config.type": host_type, "pbs-server": config}

    def get(self, host: str, key: str, default: object = None) -> object:
        return self._values.get(key, default)


def plan_for(monkeypatch: pytest.MonkeyPatch, config: dict, host_type: str = "pbs") -> dict:
    monkeypatch.setattr(
        pbs_server, "default_registry", lambda root: FakeRegistry(config, host_type)
    )
    return pbs_server.build_plan(Path("/nonexistent"), "xur")


def variant(**changes: object) -> dict:
    config = copy.deepcopy(BASE)
    config.update(changes)
    return config


def objects_by_label(plan: dict) -> dict[str, dict]:
    return {obj["label"]: obj for obj in plan["objects"]}


def test_real_inventory_builds_for_xur() -> None:
    plan = pbs_server.build_plan(REPO_ROOT, "xur")

    labels = objects_by_label(plan)
    assert "sync job sync-offsite-cinci-push" in labels
    assert labels["datastore backup"]["asserted"] == {"path": "/backup"}


def test_webhook_body_matches_the_live_alert_shape() -> None:
    plan = pbs_server.build_plan(REPO_ROOT, "xur")
    webhook = objects_by_label(plan)["webhook target Alertmanager"]

    assert json.loads(base64.b64decode(webhook["props"]["body"])) == json.loads(
        base64.b64decode(LIVE_BODY)
    )
    assert webhook["props"]["url"] == "http://helm.freender.internal:9093/api/v2/alerts"


def test_apply_defaults_to_report_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert plan_for(monkeypatch, variant())["apply"] is False
    assert plan_for(monkeypatch, variant(apply=True))["apply"] is True


def test_refuses_non_pbs_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="PBS hosts only"):
        plan_for(monkeypatch, variant(), host_type="pve")


def test_asserted_path_is_split_from_written_props(monkeypatch: pytest.MonkeyPatch) -> None:
    datastore = objects_by_label(plan_for(monkeypatch, variant()))["datastore backup"]

    assert datastore["asserted"] == {"path": "/backup"}
    assert "path" not in datastore["props"]
    assert datastore["create"] is False
    assert "keep-daily" in datastore["owned"]


def test_sync_listing_includes_push_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    sync = objects_by_label(plan_for(monkeypatch, variant()))["sync job push-a"]

    assert sync["list_args"] == ["--sync-direction", "all"]
    assert "remove-vanished" in sync["owned"]


def test_requires_a_datastore(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="at least one datastore"):
        plan_for(monkeypatch, variant(datastores={}))


def test_rejects_unknown_property(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    config["prune_jobs"]["p"]["keep-dialy"] = 7
    with pytest.raises(ValueError, match=r"unknown prune job properties \['keep-dialy'\]"):
        plan_for(monkeypatch, config)


def test_rejects_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    del config["remotes"]["zavala-push"]["fingerprint"]
    with pytest.raises(ValueError, match=r"missing required \['fingerprint'\]"):
        plan_for(monkeypatch, config)


def test_rejects_undeclared_datastore(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    config["prune_jobs"]["p"]["store"] = "other"
    with pytest.raises(ValueError, match="undeclared datastore 'other'"):
        plan_for(monkeypatch, config)


def test_rejects_undeclared_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    config["sync_jobs"]["push-a"]["remote"] = "nowhere"
    with pytest.raises(ValueError, match="undeclared remote 'nowhere'"):
        plan_for(monkeypatch, config)


def test_push_job_refuses_remove_vanished(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    config["sync_jobs"]["push-a"]["remove-vanished"] = True
    with pytest.raises(ValueError, match="must not set remove-vanished"):
        plan_for(monkeypatch, config)


def test_push_job_allows_explicit_remove_vanished_false(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    config["sync_jobs"]["push-a"]["remove-vanished"] = False
    config["sync_jobs"]["push-b"]["remove-vanished"] = False
    plan_for(monkeypatch, config)


def test_pull_job_may_remove_vanished(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant()
    config["sync_jobs"]["push-a"]["sync-direction"] = "pull"
    config["sync_jobs"]["push-a"]["remove-vanished"] = True
    plan_for(monkeypatch, config)


@pytest.mark.parametrize("prop, value", [("max-depth", 0), ("encrypted-only", True)])
def test_push_jobs_must_agree(monkeypatch: pytest.MonkeyPatch, prop: str, value: object) -> None:
    config = variant()
    config["sync_jobs"]["push-a"][prop] = value
    with pytest.raises(ValueError, match=f"push jobs disagree on {prop}"):
        plan_for(monkeypatch, config)


def test_acl_expands_roles_and_requires_declared_users(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant(
        acls=[
            {
                "path": "/datastore/backup",
                "auth-id": "pve-backup@pbs",
                "roles": ["DatastoreBackup", "DatastoreReader"],
            }
        ]
    )
    grants = plan_for(monkeypatch, config)["acls"]["grants"]
    assert [grant["role"] for grant in grants] == ["DatastoreBackup", "DatastoreReader"]

    config["users"] = []
    with pytest.raises(ValueError, match="undeclared users"):
        plan_for(monkeypatch, config)


def test_acl_outside_datastore_scope_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant(acls=[{"path": "/", "auth-id": "pve-backup@pbs", "roles": ["Admin"]}])
    with pytest.raises(ValueError, match="only /datastore ACLs"):
        plan_for(monkeypatch, config)


def test_acl_scope_is_a_path_prefix_not_a_string_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant(
        acls=[{"path": "/datastoreX", "auth-id": "pve-backup@pbs", "roles": ["Admin"]}]
    )
    with pytest.raises(ValueError, match="only /datastore ACLs"):
        plan_for(monkeypatch, config)


def test_remove_acls_cannot_overlap_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant(remove_acls=copy.deepcopy(BASE["acls"]))
    with pytest.raises(ValueError, match="overlaps acls"):
        plan_for(monkeypatch, config)


def test_notifications_disable_builtins_only(monkeypatch: pytest.MonkeyPatch) -> None:
    config = variant(notifications={"alertmanager_url": "http://am:9093/"})
    labels = objects_by_label(plan_for(monkeypatch, config))

    assert labels["webhook target Alertmanager"]["props"]["url"] == "http://am:9093/api/v2/alerts"
    assert labels["notification matcher alertmanager-matcher"]["props"]["match-severity"] == [
        "error"
    ]
    builtin = labels["sendmail target mail-to-root"]
    assert builtin["props"] == {"disable": True}
    assert builtin["owned"] == []


def test_notifications_require_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="http"):
        plan_for(monkeypatch, variant(notifications={"alertmanager_url": "helm:9093"}))


def test_unmanaged_scopes_list_declared_names_per_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = {scope["path"]: scope for scope in plan_for(monkeypatch, variant())["unmanaged"]}

    assert sorted(scopes["/config/remote"]["known"]) == ["cottonwood-push", "zavala-push"]
    assert scopes["/config/sync"]["list_args"] == ["--sync-direction", "all"]


def test_rejects_names_with_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="invalid name"):
        plan_for(monkeypatch, variant(users=["bad user"]))
