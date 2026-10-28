"""Unit tests for `zfs_automation.normalize` — the hosts.conf -> typed-plan layer.

It decides what gets snapshotted, and on which node. The failure modes worth
pinning down here are quiet ones: a validator that stops rejecting bad input lets
a malformed plan render into a sanoid config that fails at midnight on a schedule
nobody watches.

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
# safe-name and group-name guards
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
def test_require_safe_name_rejects_separators(value: str) -> None:
    with pytest.raises(ValueError, match="boom"):
        n.require_safe_name(value, "boom")


def test_require_safe_name_allows_ordinary_node_names() -> None:
    assert n.require_safe_name("bray-01_x", "boom") == "bray-01_x"


@pytest.mark.parametrize("name", ["alpha", "Alpha1", "a_b-c", "9lives", "grp-1"])
def test_migratable_group_names_accept_safe_identifiers(name: str) -> None:
    assert n.normalize_migratable_lxc_group_name(name, "ace") == name


@pytest.mark.parametrize("name", ["-alpha", "_alpha", "al pha", "al/pha", "al.pha", ""])
def test_migratable_group_names_reject_unsafe_identifiers(name: str) -> None:
    with pytest.raises(ValueError):
        n.normalize_migratable_lxc_group_name(name, "ace")


# --------------------------------------------------------------------------
# dataset path helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        ("tank/data/media", "tank"),
        ("tank", "tank"),
    ],
)
def test_dataset_pool_is_the_first_path_component(dataset: str, expected: str) -> None:
    assert n.dataset_pool(dataset) == expected


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
    assert plan.require_active_guest is None


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

    assert plan.require_active_guest == 105
    with pytest.raises(ValueError, match="require_active_lxc"):
        n.snapshot_plan_from_config({"require_active_lxc": 0}, {}, "t", "ace")


def test_snapshot_plan_per_plan_overrides_win_for_every_key() -> None:
    """Precedence was asserted for daily and recursive only, so the other six keys
    could have read from the wrong dict without any test noticing."""
    plan = n.snapshot_plan_from_config(
        {
            "hourly": 1,
            "daily": 2,
            "weekly": 3,
            "monthly": 4,
            "yearly": 5,
            "recursive": False,
            "process_children_only": False,
        },
        {
            "hourly": 90,
            "daily": 91,
            "weekly": 92,
            "monthly": 93,
            "yearly": 94,
            "recursive": True,
            "process_children_only": True,
        },
        "tank/data",
        "ace",
    )

    assert (plan.hourly, plan.daily, plan.weekly, plan.monthly, plan.yearly) == (
        "1",
        "2",
        "3",
        "4",
        "5",
    )
    assert plan.recursive is False
    assert plan.process_children_only is False


def test_snapshot_plan_falls_back_to_built_in_retention() -> None:
    """The built-in retention policy, reached only when a key is absent from *both*
    dicts. Nothing asserted these literals, so daily could have become 8 silently."""
    plan = n.snapshot_plan_from_config({}, {}, "tank/data", "ace")

    assert (plan.hourly, plan.daily, plan.weekly, plan.monthly, plan.yearly) == (
        "0",
        "7",
        "4",
        "3",
        "0",
    )
    assert plan.recursive is True
    assert plan.process_children_only is True
    assert plan.require_active_guest is None


def test_snapshot_plan_defaults_supply_the_booleans() -> None:
    """Both booleans default to True, so a defaults-only False is the one input that
    distinguishes reading `defaults` from ignoring it."""
    plan = n.snapshot_plan_from_config(
        {},
        {"recursive": False, "process_children_only": False},
        "tank/data",
        "ace",
    )

    assert plan.recursive is False
    assert plan.process_children_only is False


@pytest.mark.parametrize("key", ["recursive", "process_children_only"])
def test_snapshot_plan_rejects_a_non_boolean_flag(key: str) -> None:
    """The raise path for both flags, and that the message names the offending field
    and plan -- otherwise a misconfigured host is reported as an unattributable error."""
    with pytest.raises(ValueError, match=f"{key} for snapshot plan tank/data"):
        n.snapshot_plan_from_config({key: "maybe"}, {}, "tank/data", "ace")


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


def test_snapshot_plan_autosnap_defaults_on_and_reads_plan_then_defaults(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_defaults:
        autosnap: "no"
      snapshot_plans:
        - dataset: tank/inherits
        - dataset: tank/overrides
          autosnap: true
""",
        ),
        tmp_path,
    )

    plans = n.normalize_snapshot_plans(registry, "ace")

    assert [p.autosnap for p in plans] == [False, True]
    assert n.snapshot_plan_from_config({}, {}, "tank/x", "ace").autosnap is True


def test_snapshot_plan_rejects_a_non_boolean_autosnap() -> None:
    with pytest.raises(ValueError, match="autosnap for snapshot plan tank/x must be true or false"):
        n.snapshot_plan_from_config({"autosnap": "sometimes"}, {}, "tank/x", "ace")


def template_host_with_own_plans(own_block: str) -> str:
    return pve_host(
        "ace",
        """    zfs-automation:
      snapshot_templates:
        lxc:
          snapshot_defaults:
            daily: 21
          snapshot_plans:
            - dataset: rpool/lxc
""",
    ) + pve_host(
        "clovis",
        """    zfs-automation:
      snapshot_template: ace:lxc
"""
        + own_block,
    )


def test_snapshot_template_host_plans_follow_the_template_with_their_own_defaults(
    tmp_path: Path,
) -> None:
    """Host plans are appended after the template's, and neither side's defaults
    reach the other: a template's 21 daily must not become a replica's retention."""
    registry = registry_from(
        template_host_with_own_plans(
            """      snapshot_defaults:
        daily: 3
        autosnap: false
      snapshot_plans:
        - dataset: vault-hdd/replica/tm
"""
        ),
        tmp_path,
    )

    plans = n.normalize_snapshot_plans(registry, "clovis")

    assert [(p.dataset, p.daily, p.autosnap) for p in plans] == [
        ("rpool/lxc", "21", True),
        ("vault-hdd/replica/tm", "3", False),
    ]


def test_snapshot_template_without_host_plans_is_unchanged(tmp_path: Path) -> None:
    registry = registry_from(template_host_with_own_plans(""), tmp_path)

    plans = n.normalize_snapshot_plans(registry, "clovis")

    assert [(p.dataset, p.daily) for p in plans] == [("rpool/lxc", "21")]


def test_snapshot_template_and_host_plan_for_one_dataset_is_rejected(tmp_path: Path) -> None:
    registry = registry_from(
        template_host_with_own_plans(
            """      snapshot_plans:
        - dataset: rpool/lxc
"""
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="duplicate snapshot plan dataset rpool/lxc for clovis"):
        n.normalize_snapshot_plans(registry, "clovis")


def test_snapshot_template_host_defaults_must_still_be_a_mapping(tmp_path: Path) -> None:
    registry = registry_from(
        template_host_with_own_plans("      snapshot_defaults: [daily]\n"),
        tmp_path,
    )

    with pytest.raises(ValueError, match="snapshot_defaults must be a mapping for clovis"):
        n.normalize_snapshot_plans(registry, "clovis")


def test_snapshot_template_host_plans_must_be_a_list(tmp_path: Path) -> None:
    registry = registry_from(
        template_host_with_own_plans("      snapshot_plans: {dataset: tank/x}\n"),
        tmp_path,
    )

    with pytest.raises(ValueError, match="snapshot_plans must be a list for clovis"):
        n.normalize_snapshot_plans(registry, "clovis")


def test_snapshot_template_host_plan_errors_name_their_index_and_host(tmp_path: Path) -> None:
    registry = registry_from(
        template_host_with_own_plans(
            """      snapshot_plans:
        - dataset: tank/ok
        - not-a-mapping
"""
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="invalid snapshot plan at index 1 for clovis"):
        n.normalize_snapshot_plans(registry, "clovis")


def test_empty_snapshot_defaults_fall_back_to_built_in_retention(tmp_path: Path) -> None:
    """`snapshot_defaults:` with no value loads as None, not a mapping."""
    registry = registry_from(
        pve_host(
            "ace",
            """    zfs-automation:
      snapshot_defaults:
      snapshot_plans:
        - dataset: tank/x
""",
        ),
        tmp_path,
    )

    assert n.normalize_snapshot_plans(registry, "ace")[0].daily == "7"


def test_snapshot_template_ref_errors_name_the_key_and_host(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "clovis",
            """    zfs-automation:
      snapshot_template: nocolon
""",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="snapshot_template for clovis must use host:group format"):
        n.normalize_snapshot_plans(registry, "clovis")


def test_snapshot_template_source_without_templates_is_an_error(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host("ace")
        + pve_host(
            "clovis",
            """    zfs-automation:
      snapshot_template: ace:lxc
""",
        ),
        tmp_path,
    )

    with pytest.raises(ValueError, match="snapshot_templates must be a dict for ace"):
        n.normalize_snapshot_plans(registry, "clovis")


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

    with pytest.raises(ValueError, match="snapshot_template ace:ghost not found for bray"):
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
        (
            "          plans:\n            - {name: a, vmid: 110, dataset: rpool/a, kind: vm}\n",
            "kind must be one of lxc, qemu",
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
    """Every expanded plan must carry require_active_guest.

    A migratable container is snapshotted only on the node currently running it;
    losing that guard means both nodes snapshot the same dataset.
    """
    plans = n.expand_migratable_lxc_snapshot_group(
        group_registry(tmp_path), "ace:lxc", {"daily": 5}, "bray"
    )

    assert [(p.dataset, p.require_active_guest, p.daily) for p in plans] == [
        ("rpool/lxc/subvol-110-disk-0", 110, "5"),
        ("rpool/lxc/subvol-111-disk-0", 111, "5"),
    ]


def test_group_plan_kind_defaults_to_lxc(tmp_path: Path) -> None:
    groups = n.normalize_migratable_lxc_groups(group_registry(tmp_path), "ace")

    assert {p.kind for p in groups["lxc"].plans} == {"lxc"}


QEMU_GROUP_HOST = """    zfs-automation:
      migratable_lxc_groups:
        mixed:
          plans:
            - name: tower-appdata
              vmid: 101
              dataset: vm-flash/subvol-101-disk-1
            - name: helm-os
              vmid: 112
              kind: qemu
              dataset: vm-flash/vm-112-disk-1
"""


def test_qemu_plans_carry_their_kind_into_snapshot_plans(tmp_path: Path) -> None:
    """A VM plan that lost its kind would render the LXC guard and never snapshot."""
    registry = registry_from(
        pve_host("ace", QEMU_GROUP_HOST) + pve_host("bray", mgmt_ip="10.0.10.11/24"),
        tmp_path,
    )

    snapshots = n.expand_migratable_lxc_snapshot_group(registry, "ace:mixed", {}, "bray")

    assert [(p.dataset, p.require_active_guest, p.guest_kind) for p in snapshots] == [
        ("vm-flash/subvol-101-disk-1", 101, "lxc"),
        ("vm-flash/vm-112-disk-1", 112, "qemu"),
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
