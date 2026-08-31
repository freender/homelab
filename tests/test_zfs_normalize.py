"""Unit tests for `zfs_automation.normalize` — the hosts.conf -> typed-plan layer.

This is the largest single file in the repo (749 lines) and it decides what gets
snapshotted and what gets replicated where. Until now its only assertion-backed
coverage was per-job pause semantics (`test_zfs_replication_pause.py`); everything
else was exercised only by the offline dry-run smoke test, which asserts nothing
beyond "did not raise".

The failure modes worth pinning down here are quiet ones:
  * A validator that stops rejecting bad input lets a malformed plan render into a
    unit file that fails at 02:00 on a schedule nobody watches.
  * A dataset-path helper that stops being prefix-safe redirects replication into
    the wrong dataset, which looks like success.
  * `require_safe_authorized_key_option` guards strings that get interpolated into
    `authorized_keys` command= restrictions on the *source* host. A regression
    there is a remote-command-restriction bypass, not a cosmetic bug.

Pure functions are called directly. Anything that reads inventory goes through a
real `HostRegistry` over a temp hosts.conf, so the tests also cover the key paths
the module actually uses rather than a hand-rolled stub's idea of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.hosts import HostRegistry
from homelab.modules.zfs_automation import normalize as n

# --------------------------------------------------------------------------
# scalar and string validators
# --------------------------------------------------------------------------


def test_require_string_strips_and_rejects_blank() -> None:
    assert n.require_string("  tank/data  ", "boom") == "tank/data"
    with pytest.raises(ValueError, match="boom"):
        n.require_string("   ", "boom")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("tank/a", ["tank/a"]),
        (["tank/a", " tank/b "], ["tank/a", "tank/b"]),
    ],
)
def test_normalize_string_list_shapes(value: object, expected: list[str]) -> None:
    assert n.normalize_string_list(value, "boom") == expected


def test_normalize_string_list_rejects_a_mapping() -> None:
    """A YAML author writing list configuration as `{a: b}` must fail loudly, not silently
    normalize to the dict's keys."""
    with pytest.raises(ValueError, match="boom"):
        n.normalize_string_list({"a": "b"}, "boom")


def test_normalize_string_list_rejects_blank_items() -> None:
    with pytest.raises(ValueError, match="boom"):
        n.normalize_string_list(["tank/a", "  "], "boom")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("YES", True),
        ("1", True),
        ("on", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("OFF", False),
        (" true ", True),
    ],
)
def test_normalize_bool_accepts_documented_spellings(value: object, expected: bool) -> None:
    assert n.normalize_bool(value, not expected, "boom") is expected


def test_normalize_bool_uses_default_only_for_none() -> None:
    assert n.normalize_bool(None, True, "boom") is True
    assert n.normalize_bool(None, False, "boom") is False


@pytest.mark.parametrize("value", ["ture", "", "maybe", 2, [], "tru e"])
def test_normalize_bool_rejects_anything_else(value: object) -> None:
    """Typos must raise rather than fall through to the default.

    A silently-defaulted `recursive` flips whether child datasets get snapshotted
    at all, and nothing downstream would flag it.
    """
    with pytest.raises(ValueError, match="boom"):
        n.normalize_bool(value, True, "boom")


@pytest.mark.parametrize(("value", "expected"), [("5", 5), (5, 5), (" 7 ", 7), (110, 110)])
def test_normalize_positive_int_accepts_numeric_strings(value: object, expected: int) -> None:
    # Assert the exact value, not just positivity: a normalizer that returned a
    # constant 1 would satisfy `> 0` while silently rewriting every vmid and
    # retention count in the config.
    assert n.normalize_positive_int(value, "boom") == expected


@pytest.mark.parametrize("value", [0, -1, "0", "-3", "abc", None, ""])
def test_normalize_positive_int_rejects_non_positive(value: object) -> None:
    with pytest.raises(ValueError, match="boom"):
        n.normalize_positive_int(value, "boom")


# --------------------------------------------------------------------------
# authorized_keys injection guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        'bray"',
        "bray'",
        "bray,command=rm -rf /",
        "bray clovis",
        "bray\tclovis",
        "bray\nclovis",
        "bray\rclovis",
        "",
    ],
)
def test_require_safe_authorized_key_option_rejects_separators(value: str) -> None:
    """These characters terminate or add options in an authorized_keys line.

    Candidate names reach the source host as part of a `command=`/`restrict`
    stanza; a comma or quote getting through is an option-injection bypass.
    """
    with pytest.raises(ValueError, match="boom"):
        n.require_safe_authorized_key_option(value, "boom")


def test_require_safe_authorized_key_option_allows_ordinary_node_names() -> None:
    assert n.require_safe_authorized_key_option("bray-01_x", "boom") == "bray-01_x"


@pytest.mark.parametrize("name", ["alpha", "Alpha1", "a_b-c", "9lives"])
def test_replication_job_names_accept_safe_identifiers(name: str) -> None:
    assert n.normalize_replication_job_name(name, "ace") == name


@pytest.mark.parametrize("name", ["-alpha", "_alpha", "al pha", "al/pha", "al.pha", ""])
def test_replication_job_names_reject_unsafe_identifiers(name: str) -> None:
    """Job names become systemd unit-name fragments and file names; a dot or slash
    would silently produce a different unit than the one the config names."""
    with pytest.raises(ValueError):
        n.normalize_replication_job_name(name, "ace")


def test_migratable_group_names_use_the_same_rule() -> None:
    assert n.normalize_migratable_lxc_group_name("grp-1", "ace") == "grp-1"
    with pytest.raises(ValueError):
        n.normalize_migratable_lxc_group_name("-grp", "ace")


# --------------------------------------------------------------------------
# dataset path helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        ("tank/data/media", "tank"),
        ("tank", "tank"),
        ("zfs-pull@10.0.0.1:rpool/data", "rpool"),
        ("host:tank", "tank"),
    ],
)
def test_dataset_pool_handles_remote_and_local_forms(dataset: str, expected: str) -> None:
    assert n.dataset_pool(dataset) == expected


def test_is_remote_dataset_keys_on_the_colon() -> None:
    assert n.is_remote_dataset("host:tank/a") is True
    assert n.is_remote_dataset("tank/a") is False


@pytest.mark.parametrize(
    ("dataset", "root", "expected"),
    [
        ("backup/ace", "backup", "backup/ace"),
        ("backup", "backup", "backup"),
        ("ace/rpool", "backup", "backup/ace/rpool"),
    ],
)
def test_normalize_dataset_under_root_is_idempotent(
    dataset: str, root: str, expected: str
) -> None:
    assert n.normalize_dataset_under_root(dataset, root) == expected


def test_normalize_dataset_under_root_is_prefix_safe() -> None:
    """`backupx` is not under `backup`, despite the string prefix.

    Getting this wrong sends a replication stream to a real but unintended
    dataset, which succeeds silently.
    """
    assert n.normalize_dataset_under_root("backupx", "backup") == "backup/backupx"


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# snapshot_plan_from_config
# --------------------------------------------------------------------------


def test_snapshot_plan_inherits_defaults_and_stringifies_retention() -> None:
    plan = n.snapshot_plan_from_config(
        {},
        {"hourly": 12, "daily": 30, "weekly": 8, "monthly": 6, "yearly": 1},
        "tank/data",
        "ace",
    )

    assert (plan.hourly, plan.daily, plan.weekly, plan.monthly, plan.yearly) == (
        "12",
        "30",
        "8",
        "6",
        "1",
    )
    assert plan.recursive is True
    assert plan.process_children_only is True
    assert plan.require_active_lxc is None


def test_snapshot_plan_overrides_beat_defaults() -> None:
    plan = n.snapshot_plan_from_config(
        {"daily": 1, "recursive": False},
        {"daily": 30, "recursive": True},
        "tank/data",
        "ace",
    )

    assert plan.daily == "1"
    assert plan.recursive is False


def test_snapshot_plan_require_active_lxc_must_be_positive() -> None:
    plan = n.snapshot_plan_from_config({"require_active_lxc": 105}, {}, "t", "ace")

    assert plan.require_active_lxc == 105
    with pytest.raises(ValueError, match="require_active_lxc"):
        n.snapshot_plan_from_config({"require_active_lxc": 0}, {}, "t", "ace")


# --------------------------------------------------------------------------
# inventory-backed paths
# --------------------------------------------------------------------------


def registry_from(body: str, tmp_path: Path) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(body.lstrip(), encoding="utf-8")
    return HostRegistry(path)


HOST_HEADER = """
{name}:
  config:
    type: pve
    hostname: {name}.internal
    user: root
    sshkey: infra
  features:
"""


def pve_host(name: str, feature_block: str = "", mgmt_ip: str = "") -> str:
    body = HOST_HEADER.format(name=name)
    if mgmt_ip:
        body += f"    pve-postinstall:\n      interfaces:\n        mgmt_ip: {mgmt_ip}\n"
    if feature_block:
        body += feature_block
    if not mgmt_ip and not feature_block:
        body += "    {}\n"
    return body


def test_normalize_snapshot_plans_returns_empty_without_config(tmp_path: Path) -> None:
    registry = registry_from(pve_host("ace"), tmp_path)

    assert n.normalize_snapshot_plans(registry, "ace") == []


def test_normalize_snapshot_plans_reads_defaults_and_plans(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_defaults:
        daily: 14
      snapshot_plans:
        - dataset: rpool/data
        - dataset: tank/media
          daily: 3
""",
        ),
        tmp_path,
    )

    plans = n.normalize_snapshot_plans(registry, "ace")

    assert [p.dataset for p in plans] == ["rpool/data", "tank/media"]
    assert plans[0].daily == "14"
    assert plans[1].daily == "3"


def test_normalize_snapshot_plans_rejects_a_duplicate_dataset(tmp_path: Path) -> None:
    """Two plans for one dataset would render two competing retention policies
    into the same sanoid section; last-write-wins silently."""
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_plans:
        - dataset: rpool/data
        - dataset: rpool/data
""",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="duplicate snapshot plan dataset"):
        n.normalize_snapshot_plans(registry, "ace")


def test_normalize_snapshot_plans_rejects_a_missing_dataset(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_plans:
        - daily: 5
""",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="snapshot plan dataset required"):
        n.normalize_snapshot_plans(registry, "ace")


def test_retired_sanoid_key_fails_loudly(tmp_path: Path) -> None:
    """Migration guard: a host left on the old `sanoid:` key must error, not
    quietly deploy with zero snapshot plans."""
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      sanoid:
        templates: {}
""",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="no longer supported"):
        n.normalize_snapshot_plans(registry, "ace")


def test_snapshot_template_resolves_across_hosts(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_templates:
        lxc:
          snapshot_defaults:
            daily: 21
          snapshot_plans:
            - dataset: rpool/lxc
"""
            .rstrip("\n")
            + "\n",
        )
        + pve_host(
            "bray",
            """    zfs-automation:
      snapshot_template: ace:lxc
""",
        ),
        tmp_path,
    )

    plans = n.normalize_snapshot_plans(registry, "bray")

    assert [p.dataset for p in plans] == ["rpool/lxc"]
    assert plans[0].daily == "21"


def test_snapshot_template_missing_name_is_an_error(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_templates:
        lxc:
          snapshot_plans:
            - dataset: rpool/lxc
""",
        )
        + pve_host(
            "bray",
            """    zfs-automation:
      snapshot_template: ace:ghost
""",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="not found"):
        n.normalize_snapshot_plans(registry, "bray")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("ace:lxc", ("ace", "lxc")), ("ace.lxc", ("ace", "lxc"))],
)
def test_group_ref_accepts_colon_and_dot(ref: str, expected: tuple[str, str]) -> None:
    assert n.parse_migratable_lxc_group_ref(ref, "bray", "k") == expected


@pytest.mark.parametrize("ref", ["lxc", "", "   "])
def test_group_ref_requires_a_host_qualifier(ref: str) -> None:
    with pytest.raises(ValueError):
        n.parse_migratable_lxc_group_ref(ref, "bray", "k")


# --------------------------------------------------------------------------
# migratable LXC groups
# --------------------------------------------------------------------------

GROUP_HOST = """    zfs-automation:
      migratable_lxc_groups:
        lxc:
          plans:
            - name: traefik
              vmid: 110
              dataset: rpool/lxc/subvol-110-disk-0
            - name: redis
              vmid: 111
              dataset: rpool/lxc/subvol-111-disk-0
"""


def group_registry(tmp_path: Path) -> HostRegistry:
    return registry_from(
        pve_host("ace", GROUP_HOST)
        + pve_host("bray", mgmt_ip="10.0.10.11/24")
        + pve_host("clovis", mgmt_ip="10.0.10.12/24"),
        tmp_path,
    )


def test_migratable_groups_parse_plans(tmp_path: Path) -> None:
    groups = n.normalize_migratable_lxc_groups(group_registry(tmp_path), "ace")

    assert set(groups) == {"lxc"}
    assert [(p.name, p.vmid, p.dataset) for p in groups["lxc"].plans] == [
        ("traefik", 110, "rpool/lxc/subvol-110-disk-0"),
        ("redis", 111, "rpool/lxc/subvol-111-disk-0"),
    ]


def test_migratable_groups_absent_returns_empty(tmp_path: Path) -> None:
    assert n.normalize_migratable_lxc_groups(registry_from(pve_host("ace"), tmp_path), "ace") == {}


@pytest.mark.parametrize(
    ("plans_yaml", "match"),
    [
        ("          plans: []\n", "non-empty list"),
        (
            "          plans:\n"
            "            - {name: a, vmid: 110, dataset: rpool/a}\n"
            "            - {name: a, vmid: 111, dataset: rpool/b}\n",
            "duplicate plan name",
        ),
        (
            "          plans:\n"
            "            - {name: a, vmid: 110, dataset: rpool/a}\n"
            "            - {name: b, vmid: 111, dataset: rpool/a}\n",
            "duplicate dataset",
        ),
        (
            "          plans:\n            - {name: a, vmid: 0, dataset: rpool/a}\n",
            "vmid must be a positive integer",
        ),
        (
            "          plans:\n            - {name: a, vmid: 110}\n",
            "dataset required",
        ),
    ],
)
def test_migratable_group_plan_validation(
    tmp_path: Path, plans_yaml: str, match: str
) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n"
            "      migratable_lxc_groups:\n"
            "        lxc:\n" + plans_yaml,
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match=match):
        n.normalize_migratable_lxc_groups(registry, "ace")


def test_resolve_group_reports_an_unknown_group(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        n.resolve_migratable_lxc_group(group_registry(tmp_path), "ace:ghost", "bray", "k")


def test_snapshot_group_expansion_pins_each_plan_to_its_vmid(tmp_path: Path) -> None:
    """Every expanded plan must carry require_active_lxc.

    A migratable container is snapshotted only on the node currently running it;
    losing that guard means both nodes snapshot the same dataset and the
    replication source becomes ambiguous.
    """
    plans = n.expand_migratable_lxc_snapshot_group(
        group_registry(tmp_path), "ace:lxc", {"daily": 5}, "bray"
    )

    assert [(p.dataset, p.require_active_lxc, p.daily) for p in plans] == [
        ("rpool/lxc/subvol-110-disk-0", 110, "5"),
        ("rpool/lxc/subvol-111-disk-0", 111, "5"),
    ]


def test_snapshot_plans_can_embed_a_migratable_group(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host("ace", GROUP_HOST)
        + pve_host("bray", mgmt_ip="10.0.10.11/24")
        + pve_host(
            "clovis",
            """    zfs-automation:
      snapshot_plans:
        - dataset: rpool/data
        - migratable_lxc_group: ace:lxc
""",
        ),
        tmp_path,
    )

    plans = n.normalize_snapshot_plans(registry, "clovis")

    assert [p.dataset for p in plans] == [
        "rpool/data",
        "rpool/lxc/subvol-110-disk-0",
        "rpool/lxc/subvol-111-disk-0",
    ]


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# migratable LXC replication expansion
# --------------------------------------------------------------------------


def expand(tmp_path: Path, job_config: dict, plans: list) -> list:
    return n.expand_migratable_lxc_replication_plans(
        group_registry(tmp_path), job_config, plans, "osiris", "lxc"
    )


def base_job() -> dict:
    return {"migratable_lxc_group": "ace:lxc", "target_root": "backup/lxc"}


def test_relative_targets_are_anchored_under_target_root(tmp_path: Path) -> None:
    plans = expand(tmp_path, base_job(), [{"name": "traefik", "target": "traefik"}])

    assert plans[0].target == "backup/lxc/traefik"
    assert plans[0].source == "rpool/lxc/subvol-110-disk-0"
    assert plans[0].require_active_lxc == 110


def test_absolute_and_remote_targets_are_left_alone(tmp_path: Path) -> None:
    plans = expand(
        tmp_path,
        base_job(),
        [
            {"name": "traefik", "target": "/abs/path"},
            {"name": "redis", "target": "host:pool/redis"},
        ],
    )

    assert [p.target for p in plans] == ["/abs/path", "host:pool/redis"]


def test_target_root_trailing_slash_does_not_double_up(tmp_path: Path) -> None:
    """`backup/lxc//traefik` is not the same dataset as `backup/lxc/traefik`.

    Both the target_root rstrip and the per-target lstrip have to fire for a
    config that is sloppy on either side.
    """
    job = base_job() | {"target_root": "backup/lxc/"}

    plans = expand(tmp_path, job, [{"name": "traefik", "target": "/traefik"}])

    assert plans[0].target == "/traefik"

    plans = expand(tmp_path, job, [{"name": "traefik", "target": "traefik"}])

    assert plans[0].target == "backup/lxc/traefik"


def test_duplicate_targets_are_rejected(tmp_path: Path) -> None:
    """Two jobs replicating into one dataset would interleave streams and break
    the incremental chain for both."""
    with pytest.raises(ValueError, match="duplicate target"):
        expand(
            tmp_path,
            base_job(),
            [
                {"name": "traefik", "target": "shared"},
                {"name": "redis", "target": "shared"},
            ],
        )


def test_plan_must_exist_in_the_referenced_group(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist in group"):
        expand(tmp_path, base_job(), [{"name": "ghost", "target": "ghost"}])


# --------------------------------------------------------------------------
# source private keys and known-host refresh
# --------------------------------------------------------------------------


def keys_registry(tmp_path: Path, entries_yaml: str) -> HostRegistry:
    return registry_from(
        pve_host("ace", f"    zfs-automation:\n      source_private_keys:\n{entries_yaml}"),
        tmp_path,
    )


def test_source_private_keys_parse(tmp_path: Path) -> None:
    registry = keys_registry(
        tmp_path,
        "        - {secret: zfs-push-bray, path: /root/.ssh/homelab-zfs-push_bray}\n",
    )

    keys = n.normalize_source_private_keys(registry, "ace")

    assert [(k.secret, k.path) for k in keys] == [
        ("zfs-push-bray", "/root/.ssh/homelab-zfs-push_bray")
    ]


@pytest.mark.parametrize(
    ("path", "match"),
    [
        ("/etc/ssh/key", "must be under"),
        ("/root/.ssh/", "must be under"),
        ("relative/key", "must be under"),
        ("/root/.sshx/key", "must be under"),
    ],
)
def test_source_private_key_paths_are_confined_to_root_ssh(
    tmp_path: Path, path: str, match: str
) -> None:
    """These paths receive a rendered private key at mode 600.

    Confining them to /root/.ssh/ keeps a config typo from writing a key into a
    world-readable location.
    """
    registry = keys_registry(tmp_path, f"        - {{secret: s, path: '{path}'}}\n")

    with pytest.raises(ValueError, match=match):
        n.normalize_source_private_keys(registry, "ace")


def test_source_private_key_paths_must_be_unique(tmp_path: Path) -> None:
    """Two secrets writing the same path means one silently wins, and which one
    depends on list order."""
    registry = keys_registry(
        tmp_path,
        "        - {secret: a, path: /root/.ssh/k}\n"
        "        - {secret: b, path: /root/.ssh/k}\n",
    )

    with pytest.raises(ValueError, match="duplicate source private key path"):
        n.normalize_source_private_keys(registry, "ace")


def test_source_private_keys_absent_returns_empty(tmp_path: Path) -> None:
    assert n.normalize_source_private_keys(registry_from(pve_host("ace"), tmp_path), "ace") == ()


def refresh_registry(tmp_path: Path, entries_yaml: str) -> HostRegistry:
    return registry_from(
        pve_host("ace", f"    zfs-automation:\n      known_host_refresh:\n{entries_yaml}"),
        tmp_path,
    )


def test_known_host_refresh_applies_defaults(tmp_path: Path) -> None:
    registry = refresh_registry(tmp_path, "        - {host: bray.internal}\n")

    entries = n.normalize_known_host_refresh(registry, "ace")

    assert [(e.host, e.known_hosts, e.port) for e in entries] == [
        ("bray.internal", "/root/.ssh/known_hosts", 22)
    ]


@pytest.mark.parametrize(
    "entry",
    [
        "{host: 'bray internal'}",
        "{host: 'bray;rm -rf /'}",
        "{host: bray, port: 0}",
        "{host: bray, port: 65536}",
        "{host: bray, port: abc}",
        "{host: bray, known_hosts: /etc/ssh/known_hosts}",
    ],
)
def test_known_host_refresh_validation(tmp_path: Path, entry: str) -> None:
    """Hostname and known_hosts both end up in a `ssh-keyscan`/`ssh-keygen -R`
    command line run as root."""
    registry = refresh_registry(tmp_path, f"        - {entry}\n")

    with pytest.raises(ValueError):
        n.normalize_known_host_refresh(registry, "ace")


def test_known_host_refresh_rejects_duplicates(tmp_path: Path) -> None:
    registry = refresh_registry(
        tmp_path, "        - {host: bray}\n        - {host: bray}\n"
    )

    with pytest.raises(ValueError, match="duplicate known_host_refresh"):
        n.normalize_known_host_refresh(registry, "ace")


# --------------------------------------------------------------------------
# rendered_private_key
# --------------------------------------------------------------------------


def test_rendered_private_key_strips_env_prefix_and_quotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "body\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    key_file = tmp_path / "rendered"
    key_file.write_text(f'ZFS_PUSH_PRIVATE_KEY="{body}"\n', encoding="utf-8")
    monkeypatch.setattr(n.op_secrets, "secret_file", lambda _root, _secret: key_file)

    rendered = n.rendered_private_key(tmp_path, "zfs-push")

    assert rendered.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert rendered.endswith("-----END OPENSSH PRIVATE KEY-----\n")


def test_rendered_private_key_rejects_a_non_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 1Password miss renders an empty or placeholder value. Writing that to
    the key path would break replication with a confusing ssh error instead of
    failing at deploy time.
    """
    key_file = tmp_path / "rendered"
    key_file.write_text("ZFS_PUSH_PRIVATE_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(n.op_secrets, "secret_file", lambda _root, _secret: key_file)

    with pytest.raises(ValueError, match="did not render a private key"):
        n.rendered_private_key(tmp_path, "zfs-push")
