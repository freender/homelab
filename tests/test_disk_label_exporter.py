"""Naming tests for metrics-exporters' disk-label-textfile-exporter.

This exporter exists to delete a hand-maintained map, so the thing worth testing
is that derivation actually reproduces what a human wrote by hand -- otherwise
the replacement is a regression dressed up as a simplification.

The fixtures below are real `zpool status -LP` output captured from ace, clovis,
cottonwood and cinci on 2026-08-20. clovis is the load-bearing case: six raidz
members whose positions encode vdev order rather than device order, which is the
single fact that made "derive, don't map" viable.

Two failure modes get their own tests because both are silent and both cost the
*whole* host's metrics rather than one series:

  - node_exporter rejects an entire textfile on a duplicate metric, so two NVMe
    namespaces on one controller must not both claim the same smartctl device.
  - an OFFLINE raidz member keeps its by-id path (it has no kernel device to
    resolve to), and must still consume its position, or removing a failed disk
    silently renumbers every healthy disk behind it.

And one that is silent but fleet-wide: panels group by `disk_label` alone, so a
name that repeats across two hosts would merge two disks into one series.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "disk-label-textfile-exporter.py"

# ace/vault-hdd, captured while one member was OFFLINE after the 2026-08-10
# failure. -L cannot resolve an absent device, so that member keeps the by-id
# path it was added with.
ACE_STATUS = """  pool: rpool
 state: ONLINE
config:

\tNAME              STATE     READ WRITE CKSUM
\trpool             ONLINE       0     0     0
\t  /dev/nvme0n1p3  ONLINE       0     0     0

  pool: vault-hdd
 state: DEGRADED
config:

\tNAME                                                         STATE     READ WRITE CKSUM
\tvault-hdd                                                    DEGRADED     0     0     0
\t  raidz1-0                                                   DEGRADED     0     0     0
\t    /dev/sda1                                                ONLINE       0     0     0
\t    /dev/sdb1                                                ONLINE       0     0     0
\t    /dev/disk/by-id/ata-ST20000NM002C-3X6103_ZXA0GL7W-part1  OFFLINE      0     0     0

  pool: vm-flash
 state: ONLINE
config:

\tNAME              STATE     READ WRITE CKSUM
\tvm-flash          ONLINE       0     0     0
\t  /dev/nvme1n1p1  ONLINE       0     0     0
"""

# clovis/vault-hdd: the vdev order is deliberately not alphabetical, which is
# exactly why it proves the derivation rather than a lucky sort.
CLOVIS_STATUS = """  pool: vault-hdd
 state: ONLINE
config:

\tNAME           STATE     READ WRITE CKSUM
\tvault-hdd      ONLINE       0     0     0
\t  raidz1-0     ONLINE       0     0     0
\t    /dev/sde1  ONLINE       0     0     0
\t    /dev/sdd1  ONLINE       0     0     0
\t    /dev/sdc1  ONLINE       0     0     0
\t    /dev/sdb1  ONLINE       0     0     0
\t    /dev/sda1  ONLINE       0     0     0
\t    /dev/sdf1  ONLINE       0     0     0
"""

# ace/vault-hdd again, captured 2026-08-25 while the replacement for that same
# OFFLINE member was resilvering. `replacing-2` holds both the outgoing and the
# incoming disk for one slot, so the pool still has three members, not four.
ACE_REPLACING_STATUS = """  pool: vault-hdd
 state: DEGRADED
config:

\tNAME                                                     STATE     READ WRITE CKSUM
\tvault-hdd                                                DEGRADED     0     0     0
\t  raidz1-0                                               DEGRADED     0     0     0
\t    /dev/sda1                                            ONLINE       0     0     0
\t    /dev/sdb1                                            ONLINE       0     0     0
\t    replacing-2                                          DEGRADED     0     0     0
\t      /dev/disk/by-id/ata-ST20000NM002C-ZXA0GL7W-part1   OFFLINE      0     0     0
\t      /dev/sdc1                                          ONLINE       0     0     0  (resilvering)
"""

COTTONWOOD_STATUS = """  pool: cache
 state: ONLINE
config:

\tNAME           STATE     READ WRITE CKSUM
\tcache          ONLINE       0     0     0
\t  mirror-0     ONLINE       0     0     0
\t    /dev/sdb1  ONLINE       0     0     0
\t    /dev/sda1  ONLINE       0     0     0
"""

CINCI_STATUS = """  pool: cache
 state: ONLINE
config:

\tNAME           STATE     READ WRITE CKSUM
\tcache          ONLINE       0     0     0
\t  /dev/sda1    ONLINE       0     0     0
"""


def _load():
    spec = importlib.util.spec_from_file_location("disk_label_textfile_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return _load()


def _disk(device, rotational="1", model="", size_bytes="10000831348736"):
    return {
        "device": device,
        "smart_device": device if device.startswith("sd") else device.split("n")[0],
        "size_bytes": size_bytes,
        "rotational": rotational,
        "model": model,
        "pool": "",
        "position": "",
    }


def _labels(exporter, disks, status, root="", overrides=None):
    rows = exporter.build(
        disks,
        exporter.parse_zpool_status(status),
        root,
        overrides or {},
    )
    return {row["device"]: row["disk_label"] for row in rows}


# --- derivation reproduces the hand-written names -------------------------


def test_clovis_raidz_members_are_numbered_in_vdev_order(exporter):
    """The six-member case, and the reason this approach works at all.

    These positions were maintained by hand in ten Grafana panels. Every one of
    them falls out of the order zpool prints members, which is not device
    order: sde is D1.
    """
    disks = {name: _disk(name) for name in ("sda", "sdb", "sdc", "sdd", "sde", "sdf")}
    assert _labels(exporter, disks, CLOVIS_STATUS) == {
        "sde": "vault-hdd D1",
        "sdd": "vault-hdd D2",
        "sdc": "vault-hdd D3",
        "sdb": "vault-hdd D4",
        "sda": "vault-hdd D5",
        "sdf": "vault-hdd D6",
    }


def test_pool_names_the_disk_not_its_vdev_layout(exporter):
    """A disk is named for the pool it belongs to, never for the vdev geometry.

    Encoding geometry would mean rebuilding vault-hdd as raidz2 renames all of
    its disks and orphans their series; the pool name changes far less often.
    rpool and vm-flash are also single-member pools, which covers the
    "no position" branch.
    """
    disks = {
        "nvme0n1": _disk("nvme0n1", rotational="0"),
        "nvme1n1": _disk("nvme1n1", rotational="0"),
        "sda": _disk("sda"),
        "sdb": _disk("sdb"),
    }
    assert _labels(exporter, disks, ACE_STATUS) == {
        "nvme0n1": "rpool",
        "nvme1n1": "vm-flash",
        "sda": "vault-hdd D1",
        "sdb": "vault-hdd D2",
    }


def test_replacing_group_keeps_the_slot_it_replaces(exporter):
    """A resilvering replacement takes the failed disk's position, not a new one.

    `replacing-2` nests two leaves under one slot. Counting them as separate
    members numbered the incoming disk D4 in a three-wide raidz1 -- a member
    that does not exist -- and would have shifted every later member by one on
    a wider pool for the length of the resilver. The outgoing by-id path still
    resolves to nothing and so emits no series; the slot is what is shared.
    """
    disks = {name: _disk(name) for name in ("sda", "sdb", "sdc")}
    assert _labels(exporter, disks, ACE_REPLACING_STATUS) == {
        "sda": "vault-hdd D1",
        "sdb": "vault-hdd D2",
        "sdc": "vault-hdd D3",
    }


def test_mirror_members_are_numbered_like_any_other_pool(exporter):
    disks = {
        "sda": _disk("sda", rotational="0"),
        "sdb": _disk("sdb", rotational="0"),
    }
    assert _labels(exporter, disks, COTTONWOOD_STATUS) == {
        "sda": "cache D2",
        "sdb": "cache D1",
    }


def test_single_disk_pool_gets_no_position(exporter):
    disks = {"sda": _disk("sda", rotational="0")}
    assert _labels(exporter, disks, CINCI_STATUS) == {"sda": "cache"}


def test_pool_name_is_emitted_verbatim(exporter):
    """No cosmetic rewriting of the pool name.

    homelab_zpool_* already labels this pool `scratch`, and `zpool status` calls
    it `scratch`; a prettified `Scratch` here would be a second spelling of one
    pool in one dashboard, which is the drift this scheme exists to remove.
    """
    status = CINCI_STATUS.replace("cache", "scratch")
    disks = {"sda": _disk("sda")}
    assert _labels(exporter, disks, status) == {"sda": "scratch"}


def test_non_zfs_root_disk_is_still_called_boot(exporter):
    """cinci and cottonwood boot from plain ext4, so there is no pool to name the
    boot disk after. Without the root-filesystem fallback it would be called
    "sdb", which is exactly the unreadable legend this replaces."""
    disks = {"sda": _disk("sda", rotational="0"), "sdb": _disk("sdb", rotational="0")}
    labels = _labels(exporter, disks, CINCI_STATUS, root="sdb")
    assert labels["sdb"] == "boot"


def test_unpooled_disk_falls_back_to_its_device_name(exporter):
    disks = {"sdc": _disk("sdc")}
    assert _labels(exporter, disks, CINCI_STATUS) == {"sdc": "sdc"}


def test_model_override_names_a_disk_derivation_cannot(exporter):
    """The escape hatch, keyed by model rather than by serial.

    cottonwood's USB drive is in no pool and is not the boot disk, so nothing
    about the host says what it is. The key is matched as a prefix because the
    enclosure appends a firmware revision ("My Passport 0837") that should not
    have to be pinned.
    """
    disks = {"sdc": _disk("sdc", model="My Passport 0837")}
    labels = _labels(exporter, disks, CINCI_STATUS, overrides={"My Passport": "passport"})
    assert labels["sdc"] == "passport"


def test_longest_matching_override_prefix_wins(exporter):
    """A specific rule must not be shadowed by a general one that also matches."""
    assert (
        exporter.match_override(
            "My Passport Ultra 0837",
            {"My Passport": "passport", "My Passport Ultra": "passport ultra"},
        )
        == "passport ultra"
    )
    assert exporter.match_override("Samsung SSD 990 PRO", {"My Passport": "passport"}) == ""


# --- the silent, host-wide and fleet-wide failure modes --------------------


def test_offline_member_still_consumes_its_position(exporter):
    """ace's raidz1 has an OFFLINE third member with an unresolvable by-id path.

    It must produce no series (there is no block device to attach metrics to)
    while still holding position D3, so that replacing it does not renumber the
    two healthy disks and orphan their history.
    """
    membership = exporter.parse_zpool_status(ACE_STATUS)
    assert membership["sda"]["position"] == "D1"
    assert membership["sdb"]["position"] == "D2"
    # The offline member resolves to no kernel device, so it is absent from the
    # mapping entirely -- but D3 was consumed, not reused.
    assert sorted(key for key in membership if key.startswith("sd")) == ["sda", "sdb"]
    assert "D3" not in {entry["position"] for entry in membership.values()}


def test_names_collide_across_hosts_and_must_be_grouped_by_host(exporter):
    """The name is deliberately host-relative: ace and clovis both call a disk
    "vault-hdd D1".

    This is not a defect to be fixed by re-adding a host prefix -- it is why
    panels must group by (host, disk_label) rather than disk_label alone, which
    makes a cross-host merge structurally impossible instead of test-enforced.
    The `host` label the scrape config attaches is the disambiguator, and it is
    the same one every non-disk panel in the dashboard already uses.
    """
    ace = _labels(exporter, {"sda": _disk("sda")}, ACE_STATUS)
    clovis = _labels(exporter, {"sde": _disk("sde")}, CLOVIS_STATUS)
    assert ace["sda"] == clovis["sde"] == "vault-hdd D1"


def test_shared_smartctl_device_does_not_duplicate_a_series(exporter):
    """Two namespaces on one NVMe controller collapse to the same smartctl
    device. node_exporter rejects the *entire* textfile on a duplicate metric,
    which would unlabel every disk on the host, so the second one is dropped."""
    rows = [
        {**_disk("nvme0n1", rotational="0"), "disk_label": "rpool", "pool": "rpool"},
        {**_disk("nvme0n2", rotational="0"), "disk_label": "nvme0n2"},
    ]
    for row in rows:
        row["smart_device"] = "nvme0"
    output = exporter.render(rows)
    smart = [ln for ln in output.splitlines() if ln.startswith("homelab_smart_disk_label")]
    assert len(smart) == 1
    # Both disks still get their own node_exporter-keyed series; only the
    # ambiguous smartctl view is collapsed.
    assert len([ln for ln in output.splitlines() if ln.startswith("homelab_disk_label{")]) == 2


# --- label hygiene --------------------------------------------------------


def _render_one(exporter, **kw):
    rows = exporter.build(
        {"sda": _disk("sda", **kw)}, exporter.parse_zpool_status(ACE_STATUS), "", {}
    )
    return exporter.render(rows), rows


def test_no_serial_label_is_emitted(exporter):
    """Serial is not the join key and is ambiguous across subsystems: for a USB
    disk the kernel and smartctl report different values, and some bridges
    report a placeholder. Emitting one would reintroduce the identifier this
    design exists to avoid."""
    output, _ = _render_one(exporter)
    assert "serial" not in output


def test_no_host_label_is_emitted(exporter):
    """The scrape config attaches `host`; emitting our own would land as
    `exported_host` and break the (host, device) join every panel depends on."""
    output, rows = _render_one(exporter)
    for line in output.splitlines():
        if line.startswith("homelab_"):
            assert 'host="' not in line
    # ...and it does not appear in the label *value* either: the panel prefixes
    # `{{host}}` itself, exactly as every non-disk panel does.
    assert rows[0]["disk_label"] == "vault-hdd D1"


def test_capacity_is_published_as_exact_bytes_not_a_rounded_string(exporter):
    """Capacity is data, not part of the name. Printing it in the label would
    need a rounding heuristic, and every tolerance loose enough to turn
    20,000,588,955,648 into "20TB" also turns a 1.92TB enterprise SSD into
    "2TB"."""
    output, rows = _render_one(exporter, size_bytes="20000588955648")
    assert 'size_bytes="20000588955648"' in output
    assert "TB" not in rows[0]["disk_label"]


def test_rotational_zero_survives_empty_label_filtering(exporter):
    """Empty labels are dropped, and rotational="0" must not be mistaken for one
    -- every NVMe panel filters on exactly that value."""
    output, _ = _render_one(exporter, rotational="0")
    assert 'rotational="0"' in output
