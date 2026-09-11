"""Unit tests for `zfs_automation.access` and the replication-job helpers.

`access.py` decides two things that fail quietly:

  * `resolve_pools` picks which pools the module manages. Get it wrong and a pool
    silently stops being scrubbed -- there is no error, just no scrub.
  * `normalize_push_target_access` builds the `authorized_keys` restrictions on a
    *receive* host. Every string here is interpolated into a `command=`/`from=`
    option, so a validator that stops rejecting a separator is a remote-command
    bypass, not a cosmetic bug.

Both were previously carried only by `test_dry_run_all_modules.py`, which asserts
`exit_code == 0` and nothing else. Pure helpers are called directly; anything that
reads inventory goes through a real `HostRegistry` over a temp hosts.conf.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.hosts import HostRegistry
from homelab.modules.zfs_automation import access as a
from homelab.modules.zfs_automation import replication as r
from homelab.modules.zfs_automation.types import ReplicationJob, ReplicationPlan

ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample homelab-zfs-push test"


def registry_from(body: str, tmp_path: Path) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(body.lstrip(), encoding="utf-8")
    return HostRegistry(path)


def pve_host(name: str, feature_block: str = "") -> str:
    body = (
        f"{name}:\n"
        "  config:\n"
        "    type: pve\n"
        f"    hostname: {name}.internal\n"
        "    user: root\n"
        "    sshkey: infra\n"
        "  features:\n"
    )
    return body + (feature_block or "    {}\n")


def job(*plans: ReplicationPlan) -> ReplicationJob:
    return ReplicationJob(
        name="j",
        schedule="*-*-* 02:30:00",
        plans=plans,
        syncoid_options=(),
        delete_target_snapshots=True,
    )


# --------------------------------------------------------------------------
# pool derivation
# --------------------------------------------------------------------------


def test_unique_pools_dedupes_and_keeps_first_seen_order() -> None:
    datasets = ["tank/a", "cache/b", "tank/c", "rpool/d", "cache/e"]

    assert a.unique_pools(datasets) == ["tank", "cache", "rpool"]


def test_unique_pools_of_nothing_is_empty() -> None:
    assert a.unique_pools([]) == []


def test_local_replication_datasets_skips_remote_endpoints() -> None:
    """A `host:dataset` endpoint lives on another host's pools, so it must not
    pull that pool into this host's managed set."""
    jobs = [
        job(ReplicationPlan(source="tank/local", target="cinci:cache/remote")),
        job(ReplicationPlan(source="neo:tank/remote", target="cache/local")),
    ]

    assert a.local_replication_datasets(jobs) == ["tank/local", "cache/local"]


def test_local_replication_datasets_tolerates_an_absent_source() -> None:
    """`source` is optional -- an empty one must be dropped, not emitted as ''."""
    jobs = [job(ReplicationPlan(source="", target="cache/only"))]

    assert a.local_replication_datasets(jobs) == ["cache/only"]


def test_local_replication_datasets_of_no_jobs_is_empty() -> None:
    assert a.local_replication_datasets([]) == []


def test_resolve_pools_prefers_an_explicit_list(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n"
            "      pools:\n"
            "        - tank\n"
            "        - rpool\n"
            "      snapshot_plans:\n"
            "        - dataset: cache/appdata\n",
        ),
        tmp_path,
    )

    # cache/appdata would otherwise contribute `cache`; the explicit list wins.
    assert a.resolve_pools(registry, "ace") == ["tank", "rpool"]


def test_resolve_pools_derives_from_snapshot_and_replication_datasets(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n"
            "      snapshot_plans:\n"
            "        - dataset: cache/appdata\n"
            "      replication_jobs:\n"
            "        nightly:\n"
            "          plans:\n"
            "            - source: tank/media\n"
            "              target: cinci:backup/media\n",
        ),
        tmp_path,
    )

    # `cache` from the snapshot plan, `tank` from the local replication source;
    # `backup` is remote and must not appear.
    assert a.resolve_pools(registry, "ace") == ["cache", "tank"]


def test_resolve_pools_falls_back_to_cache_when_nothing_is_configured(tmp_path: Path) -> None:
    registry = registry_from(pve_host("ace", "    zfs-automation:\n      scrub: true\n"), tmp_path)

    assert a.resolve_pools(registry, "ace") == ["cache"]


# --------------------------------------------------------------------------
# push_target_access template expansion
# --------------------------------------------------------------------------


def test_expand_access_template_returns_config_untouched_without_a_ref(tmp_path: Path) -> None:
    registry = registry_from(pve_host("ace"), tmp_path)
    config = {"user": "zfs-push", "datasets": ["cache/a"]}

    assert a.expand_access_template(registry, "ace", config) is config


def test_expand_access_template_lets_the_host_override_the_template(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n"
            "      push_target_access_templates:\n"
            "        neo-data:\n"
            "          user: zfs-push\n"
            "          datasets:\n"
            "            - cache/neo/pictures\n",
        )
        + pve_host("neo"),
        tmp_path,
    )

    expanded = a.expand_access_template(
        registry,
        "neo",
        {"template": "ace:neo-data", "user": "override"},
    )

    assert expanded["user"] == "override"
    assert expanded["datasets"] == ["cache/neo/pictures"]
    # The reference keys themselves must not survive into the expanded config.
    assert "template" not in expanded


def test_expand_access_template_rejects_a_missing_template(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n      push_target_access_templates:\n        other: {}\n",
        )
        + pve_host("neo"),
        tmp_path,
    )

    with pytest.raises(ValueError, match="not found"):
        a.expand_access_template(registry, "neo", {"template": "ace:nope"})


def test_expand_access_template_rejects_a_host_with_no_templates_block(tmp_path: Path) -> None:
    registry = registry_from(pve_host("ace") + pve_host("neo"), tmp_path)

    with pytest.raises(ValueError, match="must be a dict"):
        a.expand_access_template(registry, "neo", {"template": "ace:neo-data"})


# --------------------------------------------------------------------------
# allowed_pushers
# --------------------------------------------------------------------------


def test_normalize_pusher_builds_a_pusher() -> None:
    pusher = a.normalize_pusher(
        {"name": "ace", "from": "10.0.40.0/24", "public_key": ED25519},
        0,
        "neo",
    )

    assert (pusher.name, pusher.from_address) == ("ace", "10.0.40.0/24")
    assert pusher.public_key == ED25519


def test_normalize_pusher_rejects_a_non_mapping() -> None:
    with pytest.raises(ValueError, match="invalid push target allowed_pusher at index 2"):
        a.normalize_pusher("ace", 2, "neo")


@pytest.mark.parametrize("key", ["ssh-rsa AAAAB3Nza", "", "ecdsa-sha2-nistp256 AAAA"])
def test_normalize_pusher_requires_an_ed25519_key(key: str) -> None:
    """RSA and ECDSA are refused outright rather than accepted and weakly trusted."""
    with pytest.raises(ValueError):
        a.normalize_pusher({"name": "ace", "from": "*", "public_key": key}, 0, "neo")


def test_normalize_pusher_accepts_a_security_key_backed_ed25519() -> None:
    key = "sk-ssh-ed25519@openssh.com AAAAGnNr example"

    assert a.normalize_pusher({"name": "a", "from": "*", "public_key": key}, 0, "neo").public_key

@pytest.mark.parametrize("bad", ['ace" command="rm -rf /', "ace\nfrom=*", "ace,from=*"])
def test_normalize_pusher_rejects_authorized_keys_separators(bad: str) -> None:
    """These strings land inside an authorized_keys option list; a quote, comma or
    newline that survives here escapes the restriction."""
    with pytest.raises(ValueError):
        a.normalize_pusher({"name": bad, "from": "*", "public_key": ED25519}, 0, "neo")


def test_normalize_pushers_requires_a_non_empty_list() -> None:
    for config in ({}, {"allowed_pushers": []}, {"allowed_pushers": "ace"}):
        with pytest.raises(ValueError, match="non-empty list"):
            a.normalize_pushers(config, "neo")


def test_normalize_pushers_preserves_order_and_reports_the_failing_index() -> None:
    good = {"name": "ace", "from": "*", "public_key": ED25519}
    assert [p.name for p in a.normalize_pushers({"allowed_pushers": [good]}, "neo")] == ["ace"]

    with pytest.raises(ValueError, match="index 1"):
        a.normalize_pushers({"allowed_pushers": [good, "nope"]}, "neo")


# --------------------------------------------------------------------------
# normalize_push_target_access
# --------------------------------------------------------------------------


ACCESS_BLOCK = (
    "    zfs-automation:\n"
    "      push_target_access:\n"
    "{extra}"
    "        datasets:\n"
    "          - cache/neo/pictures\n"
    "        allowed_pushers:\n"
    "          - name: ace\n"
    "            from: '*'\n"
    f"            public_key: {ED25519}\n"
)


def test_push_target_access_is_none_when_unconfigured(tmp_path: Path) -> None:
    registry = registry_from(pve_host("neo"), tmp_path)

    assert a.normalize_push_target_access(registry, "neo") is None


def test_push_target_access_defaults_the_user_and_stays_enabled(tmp_path: Path) -> None:
    registry = registry_from(pve_host("neo", ACCESS_BLOCK.format(extra="")), tmp_path)

    result = a.normalize_push_target_access(registry, "neo")

    assert result is not None
    assert result.enabled is True
    assert result.user == "zfs-push"
    assert result.datasets == ("cache/neo/pictures",)
    assert [p.name for p in result.pushers] == ["ace"]


def test_push_target_access_hides_a_disabled_block_unless_asked(tmp_path: Path) -> None:
    """`include_disabled` exists so the installer can still tear down the account;
    ordinary callers must see None and skip it entirely."""
    registry = registry_from(
        pve_host("neo", ACCESS_BLOCK.format(extra="        enabled: false\n")),
        tmp_path,
    )

    assert a.normalize_push_target_access(registry, "neo") is None

    disabled = a.normalize_push_target_access(registry, "neo", include_disabled=True)
    assert disabled is not None
    assert disabled.enabled is False


def test_push_target_access_requires_datasets(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "neo",
            "    zfs-automation:\n"
            "      push_target_access:\n"
            "        allowed_pushers:\n"
            "          - name: ace\n"
            "            from: '*'\n"
            f"            public_key: {ED25519}\n",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="datasets is required"):
        a.normalize_push_target_access(registry, "neo")


def test_push_target_access_rejects_a_non_mapping(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host("neo", "    zfs-automation:\n      push_target_access: nope\n"),
        tmp_path,
    )

    with pytest.raises(ValueError, match="must be a mapping"):
        a.normalize_push_target_access(registry, "neo")


# --------------------------------------------------------------------------
# replication helpers extracted from normalize_replication_config
# --------------------------------------------------------------------------


@pytest.mark.parametrize("legacy", ["replication_plans", "replication"])
def test_legacy_replication_keys_fail_loudly(legacy: str, tmp_path: Path) -> None:
    """Ignoring these would deploy a host with no replication at all, which is
    indistinguishable from success until a restore is needed."""
    registry = registry_from(
        pve_host("ace", f"    zfs-automation:\n      {legacy}:\n        - source: tank/a\n"),
        tmp_path,
    )

    with pytest.raises(ValueError, match="no longer supported"):
        r.reject_legacy_replication_keys(registry, "ace")


def test_reject_legacy_replication_keys_passes_a_modern_config(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host("ace", "    zfs-automation:\n      replication_jobs: {}\n"), tmp_path
    )

    assert r.reject_legacy_replication_keys(registry, "ace") is None


def test_normalize_explicit_plan_requires_both_ends() -> None:
    plan = r.normalize_explicit_plan({"source": " tank/a ", "target": "cinci:b/c"}, 0, "ace", "j")
    assert (plan.source, plan.target) == ("tank/a", "cinci:b/c")

    with pytest.raises(ValueError, match="must specify source"):
        r.normalize_explicit_plan({"target": "cinci:b/c"}, 0, "ace", "j")
    with pytest.raises(ValueError, match="plan target required"):
        r.normalize_explicit_plan({"source": "tank/a"}, 0, "ace", "j")
    with pytest.raises(ValueError, match="invalid plan at index 3"):
        r.normalize_explicit_plan("tank/a", 3, "ace", "j")


def test_normalize_job_plans_rejects_a_non_list() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        r.normalize_job_plans(None, {"plans": {"source": "a"}}, "ace", "j")


def test_normalize_job_plans_reads_explicit_plans_in_order() -> None:
    plans = r.normalize_job_plans(
        None,
        {
            "plans": [
                {"source": "tank/a", "target": "cinci:b/a"},
                {"source": "tank/b", "target": "cinci:b/b"},
            ]
        },
        "ace",
        "j",
    )

    assert [p.source for p in plans] == ["tank/a", "tank/b"]


def test_build_replication_job_returns_none_for_a_retired_job() -> None:
    config = {"enabled": False, "plans": [{"source": "tank/a", "target": "cinci:b/a"}]}

    assert r.build_replication_job(None, "ace", "j", config, include_disabled=False) is None

    kept = r.build_replication_job(None, "ace", "j", config, include_disabled=True)
    assert kept is not None and kept.name == "j"


def test_build_replication_job_applies_documented_defaults() -> None:
    built = r.build_replication_job(
        None,
        "ace",
        "nightly",
        {"plans": [{"source": "tank/a", "target": "cinci:b/a"}]},
        include_disabled=False,
    )

    assert built is not None
    assert built.schedule == "*-*-* 02:30:00"
    assert built.delete_target_snapshots is True
    assert built.paused is False
    assert built.syncoid_options == ()


def test_build_replication_job_keeps_a_paused_job_in_the_list() -> None:
    """Paused is not retired: the units stay managed, so the job must still be
    returned rather than dropped like `enabled: false`."""
    built = r.build_replication_job(
        None,
        "ace",
        "nightly",
        {"paused": True, "plans": [{"source": "tank/a", "target": "cinci:b/a"}]},
        include_disabled=False,
    )

    assert built is not None and built.paused is True
