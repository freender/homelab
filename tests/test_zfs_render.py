"""The active-guest guard in the rendered snapshot script.

A migratable guest's datasets must be touched only on the node running it. The
guard is a `systemctl is-active` on a unit that differs by guest kind; a wrong
unit name renders a script that skips the guest everywhere, silently.
"""

from __future__ import annotations

import pytest

from homelab.modules.zfs_automation.render import active_guest_unit, build_snapshot_script
from homelab.modules.zfs_automation.types import SnapshotPlan


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("lxc", ("LXC 101", "pve-container@101.service")),
        ("qemu", ("VM 112", "112.scope")),
    ],
)
def test_active_guest_unit_per_kind(kind: str, expected: tuple[str, str]) -> None:
    vmid = int(expected[0].split()[1])
    assert active_guest_unit(kind, vmid) == expected


def snapshot_plan(**kwargs) -> SnapshotPlan:
    return SnapshotPlan(
        dataset="vm-flash/vm-112-disk-1",
        hourly="0",
        daily="7",
        weekly="4",
        monthly="2",
        yearly="0",
        **kwargs,
    )


def test_snapshot_script_guards_a_vm_on_its_scope() -> None:
    script = build_snapshot_script([snapshot_plan(require_active_guest=112, guest_kind="qemu")])

    assert "if systemctl is-active --quiet 112.scope; then" in script
    assert "echo 'Active VM 112; snapshotting vm-flash/vm-112-disk-1'" in script
    assert "echo 'Skipping vm-flash/vm-112-disk-1; VM 112 is not active locally'" in script
    assert "pve-container@" not in script


def test_snapshot_script_keeps_the_lxc_guard_by_default() -> None:
    script = build_snapshot_script([snapshot_plan(require_active_guest=101)])

    assert "if systemctl is-active --quiet pve-container@101.service; then" in script
    assert "echo 'Skipping vm-flash/vm-112-disk-1; LXC 101 is not active locally'" in script


def test_unguarded_snapshot_plan_has_no_active_check() -> None:
    script = build_snapshot_script([snapshot_plan()])

    assert "systemctl is-active" not in script
    assert "Including snapshot plan: vm-flash/vm-112-disk-1" in script


def test_snapshot_plan_takes_snapshots_by_default() -> None:
    script = build_snapshot_script([snapshot_plan()])

    assert "autosnap = yes\nautoprune = yes\n" in script
    assert "autosnap = no" not in script


def test_prune_only_plan_renders_autosnap_off_and_says_so() -> None:
    script = build_snapshot_script([snapshot_plan(autosnap=False)])

    assert "autosnap = no\nautoprune = yes\n" in script
    assert "autosnap = yes" not in script
    assert "echo 'Including prune-only plan: vm-flash/vm-112-disk-1'" in script

