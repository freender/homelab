"""Naming tests for metrics-exporters' disk-label-textfile-exporter.

This exporter exists to delete a hand-maintained map, so the thing worth testing
is that derivation actually reproduces what a human wrote by hand -- otherwise
the replacement is a regression dressed up as a simplification.

The fixtures below are real `zpool status -LP` output captured from ace, clovis,
cottonwood and cinci on 2026-08-20, and the expected names are the ones the
Grafana dashboard carried in its overrides on that date. clovis is the load
bearing case: six raidz members whose names encode vdev order, which is the
single fact that made "derive, don't map" viable.

Two failure modes get their own tests because both are silent and both cost the
*whole* host's metrics rather than one series:

  - node_exporter rejects an entire textfile on a duplicate metric, so two NVMe
    namespaces on one controller must not both claim the same smartctl device.
  - an OFFLINE raidz member keeps its by-id path (it has no kernel device to
    resolve to), and must still consume its position, or removing a failed disk
    silently renumbers every healthy disk behind it.
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


def _disk(device, size_bytes, rotational="1", model=""):
    return {
        "device": device,
        "smart_device": device if device.startswith("sd") else device.split("n")[0],
        "size_bytes": str(size_bytes),
        "rotational": rotational,
        "model": model,
        "pool": "",
        "vdev": "",
        "position": "",
    }


def _labels(exporter, disks, status, host, root="", overrides=None):
    rows = exporter.build(
        disks,
        exporter.parse_zpool_status(status),
        root,
        host,
        overrides or {},
    )
    return {row["device"]: row["disk_label"] for row in rows}


# --- capacity ------------------------------------------------------------


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        # Decimal capacities: a 20 TB drive really is 20.0006 TB, and snapping
        # to the marketing number is what makes the legend readable.
        (20_000_588_955_648, "20TB"),
        (12_000_138_625_024, "12TB"),
        (10_000_831_348_736, "10TB"),
        (4_000_787_030_016, "4TB"),
        (2_000_398_934_016, "2TB"),
        (1_024_209_543_168, "1TB"),
        (1_000_204_886_016, "1TB"),
        (512_110_190_592, "512GB"),
        (500_107_862_016, "500GB"),
        (256_060_514_304, "256GB"),
        # Genuinely fractional capacity must not be snapped to a wrong integer.
        (1_500_000_000_000, "1.5TB"),
        (0, ""),
    ],
)
def test_capacity_reads_as_the_number_on_the_box(exporter, size_bytes, expected):
    assert exporter.format_size(size_bytes) == expected


# --- derivation reproduces the hand-written names -------------------------


def test_clovis_raidz_members_match_the_hand_written_names(exporter):
    """The six-member case, and the reason this approach works at all.

    These names were maintained by hand in ten Grafana panels. Every one of them
    falls out of vdev order, which is not the same as device order: sde is D1.
    """
    disks = {
        "sda": _disk("sda", 10_000_831_348_736),
        "sdb": _disk("sdb", 10_000_831_348_736),
        "sdc": _disk("sdc", 10_000_831_348_736),
        "sdd": _disk("sdd", 10_000_831_348_736),
        "sde": _disk("sde", 12_000_138_625_024),
        "sdf": _disk("sdf", 10_000_831_348_736),
    }
    assert _labels(exporter, disks, CLOVIS_STATUS, "Clovis") == {
        "sde": "Clovis Z1 D1 (12TB)",
        "sdd": "Clovis Z1 D2 (10TB)",
        "sdc": "Clovis Z1 D3 (10TB)",
        "sdb": "Clovis Z1 D4 (10TB)",
        "sda": "Clovis Z1 D5 (10TB)",
        "sdf": "Clovis Z1 D6 (10TB)",
    }


def test_pool_purpose_beats_vdev_geometry(exporter):
    """rpool and vm-flash are named for what they are for, not how they are built.

    Both are single-disk pools with no container vdev, so they also cover the
    "leaf directly under the pool" parse path, which is a different branch from
    the raidz case above.
    """
    disks = {
        "nvme0n1": _disk("nvme0n1", 500_107_862_016, rotational="0"),
        "nvme1n1": _disk("nvme1n1", 2_000_398_934_016, rotational="0"),
        "sda": _disk("sda", 20_000_588_955_648),
        "sdb": _disk("sdb", 20_000_588_955_648),
    }
    labels = _labels(exporter, disks, ACE_STATUS, "Ace")
    assert labels["nvme0n1"] == "Ace Boot (500GB)"
    assert labels["nvme1n1"] == "Ace VM-Flash (2TB)"


def test_mirror_members_are_lettered_not_numbered(exporter):
    """A mirror is a set of equals, so A/B reads better than D1/D2 -- and that is
    what the hand-written cottonwood names already used."""
    disks = {
        "sda": _disk("sda", 4_000_787_030_016, rotational="0"),
        "sdb": _disk("sdb", 4_000_787_030_016, rotational="0"),
    }
    assert _labels(exporter, disks, COTTONWOOD_STATUS, "Cottonwood") == {
        "sda": "Cottonwood Cache B (4TB)",
        "sdb": "Cottonwood Cache A (4TB)",
    }


def test_single_disk_pool_gets_no_position(exporter):
    disks = {"sda": _disk("sda", 2_048_408_248_320, rotational="0")}
    assert _labels(exporter, disks, CINCI_STATUS, "Cinci") == {"sda": "Cinci Cache (2TB)"}


def test_non_zfs_root_disk_is_still_called_boot(exporter):
    """cinci and cottonwood boot from plain ext4, so there is no pool to name the
    boot disk after. Without the root-filesystem fallback it would be called
    "Cinci sdb", which is exactly the unreadable legend this replaces."""
    disks = {
        "sda": _disk("sda", 2_048_408_248_320, rotational="0"),
        "sdb": _disk("sdb", 512_110_190_592, rotational="0"),
    }
    labels = _labels(exporter, disks, CINCI_STATUS, "Cinci", root="sdb")
    assert labels["sdb"] == "Cinci Boot (512GB)"


def test_unpooled_disk_falls_back_to_its_device_name(exporter):
    disks = {"sdc": _disk("sdc", 1_000_204_886_016)}
    assert _labels(exporter, disks, CINCI_STATUS, "Cottonwood") == {
        "sdc": "Cottonwood sdc (1TB)"
    }


def test_model_override_names_a_disk_derivation_cannot(exporter):
    """The escape hatch, keyed by model rather than by serial.

    cottonwood's USB drive is in no pool and is not the boot disk, so nothing
    about the host says what it is. The key is matched as a prefix because the
    enclosure appends a firmware revision ("My Passport 0837") that should not
    have to be pinned.
    """
    disks = {"sdc": _disk("sdc", 1_000_204_886_016, model="My Passport 0837")}
    labels = _labels(
        exporter,
        disks,
        CINCI_STATUS,
        "Cottonwood",
        overrides={"My Passport": "Passport"},
    )
    assert labels["sdc"] == "Cottonwood Passport (1TB)"


def test_longest_matching_override_prefix_wins(exporter):
    """A specific rule must not be shadowed by a general one that also matches."""
    assert (
        exporter.match_override(
            "My Passport Ultra 0837",
            {"My Passport": "Passport", "My Passport Ultra": "Passport Ultra"},
        )
        == "Passport Ultra"
    )
    assert exporter.match_override("Samsung SSD 990 PRO", {"My Passport": "Passport"}) == ""


# --- the two silent, host-wide failure modes ------------------------------


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
    assert [key for key in membership if key.startswith("sd")] == ["sda", "sdb"]
    assert "D3" not in {entry["position"] for entry in membership.values()}


def test_shared_smartctl_device_does_not_duplicate_a_series(exporter):
    """Two namespaces on one NVMe controller collapse to the same smartctl
    device. node_exporter rejects the *entire* textfile on a duplicate metric,
    which would unlabel every disk on the host, so the second one is dropped."""
    rows = [
        {
            "device": "nvme0n1",
            "smart_device": "nvme0",
            "disk_label": "Ace Boot (500GB)",
            "pool": "rpool",
            "vdev": "",
            "position": "",
            "role": "boot",
            "rotational": "0",
            "model": "",
            "size_bytes": "500107862016",
        },
        {
            "device": "nvme0n2",
            "smart_device": "nvme0",
            "disk_label": "Ace Boot 2 (500GB)",
            "pool": "",
            "vdev": "",
            "position": "",
            "role": "unassigned",
            "rotational": "0",
            "model": "",
            "size_bytes": "500107862016",
        },
    ]
    output = exporter.render(rows)
    smart_lines = [
        line for line in output.splitlines() if line.startswith("homelab_smart_disk_label")
    ]
    assert len(smart_lines) == 1
    # Both disks still get their own node_exporter-keyed series; only the
    # ambiguous smartctl view is collapsed.
    disk_lines = [line for line in output.splitlines() if line.startswith("homelab_disk_label{")]
    assert len(disk_lines) == 2


# --- label hygiene --------------------------------------------------------


def test_no_serial_label_is_emitted(exporter):
    """Serial is not the join key and is ambiguous across subsystems: for a USB
    disk the kernel and smartctl report different values, and some bridges
    report a placeholder. Emitting one would reintroduce the identifier this
    design exists to avoid."""
    rows = exporter.build(
        {"sda": _disk("sda", 20_000_588_955_648)},
        exporter.parse_zpool_status(ACE_STATUS),
        "",
        "Ace",
        {},
    )
    output = exporter.render(rows)
    assert "serial" not in output


def test_no_host_label_is_emitted(exporter):
    """The scrape config attaches `host`; emitting our own would land as
    `exported_host` and break the (host, device) join every panel depends on."""
    rows = exporter.build(
        {"sda": _disk("sda", 20_000_588_955_648)},
        exporter.parse_zpool_status(ACE_STATUS),
        "",
        "Ace",
        {},
    )
    for line in exporter.render(rows).splitlines():
        if line.startswith("homelab_"):
            assert 'host="' not in line
    # ...but the host name still appears inside the label *value*, which is what
    # makes the legend readable.
    assert rows[0]["disk_label"].startswith("Ace ")


def test_rotational_zero_survives_empty_label_filtering(exporter):
    """Empty labels are dropped, and rotational="0" must not be mistaken for one
    -- every NVMe panel filters on exactly that value."""
    rows = exporter.build(
        {"nvme1n1": _disk("nvme1n1", 2_000_398_934_016, rotational="0")},
        exporter.parse_zpool_status(ACE_STATUS),
        "",
        "Ace",
        {},
    )
    assert 'rotational="0"' in exporter.render(rows)
