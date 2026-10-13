"""Compare a `pve-guest` spec with the guest's current Proxmox config.

Read-only by design: every difference is labelled with what applying it would take,
and nothing here ever proposes removing a key the spec does not declare (a live key
the spec does not manage is reported as unmanaged and left alone).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .build import command, reattach
from .pveconf import (
    LiveDisk,
    LiveHa,
    LiveNet,
    LiveReplication,
    format_size,
    parse_disk,
    parse_net,
    same_value,
    split_prop,
    storage_of,
    volume_of,
)
from .spec import DiskSpec, GuestSpec, NetSpec

LIVE = "applies live"
REBOOT = "needs reboot"
REFUSED = "refused"
CONFIG = "config only"
CLUSTER = "cluster config"
UNMANAGED = "unmanaged (never removed)"

# Per-guest identity values: never compared, never reported.
IDENTITY = frozenset({"digest", "meta", "smbios1", "vmgenid", "lock", "parent"})

# What a missing key means.
DEFAULTS: dict[str, dict[str, str]] = {
    "qemu": {
        "sockets": "1",
        "numa": "0",
        "onboot": "0",
        "tablet": "1",
        "agent": "0",
        "protection": "0",
    },
    "lxc": {"onboot": "0", "unprivileged": "0", "swap": "512", "arch": "amd64", "protection": "0"},
}

QEMU_LIVE = frozenset({"balloon", "onboot", "name", "tablet", "startup", "tags", "protection"})
LXC_REBOOT = frozenset({"features", "ostype", "hostname"})
LXC_REFUSED = frozenset({"arch", "unprivileged"})
UNUSED_RE = re.compile(r"^unused\d+$")


@dataclass(frozen=True)
class Diff:
    key: str
    want: str
    have: str
    impact: str
    # The command that settles this row, shown under it; still report-only.
    fix: str | None = None


def _show(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def diff(key: str, want: object, have: object, impact: str) -> Diff:
    return Diff(key, _show(want), _show(have), impact)


def scalar_impact(kind: str, key: str) -> str:
    if kind == "qemu":
        return LIVE if key in QEMU_LIVE else REBOOT
    if key in LXC_REFUSED:
        return REFUSED
    if key in LXC_REBOOT or key.startswith("dev"):
        return REBOOT
    return LIVE


def scalar_diffs(spec: GuestSpec, live: dict[str, str]) -> list[Diff]:
    defaults = DEFAULTS[spec.kind]
    rows = []
    for key, want in spec.scalars.items():
        have = live.get(key, defaults.get(key))
        if not same_value(key, want, have):
            rows.append(diff(key, want, have, scalar_impact(spec.kind, key)))
    return rows


def efidisk_diffs(spec: GuestSpec, live: dict[str, str]) -> list[Diff]:
    if not spec.efidisk:
        return []
    have = split_prop(live["efidisk0"]) if "efidisk0" in live else {}
    return [
        diff(f"efidisk0.{key}", want, have.get(key), REFUSED)
        for key, want in spec.efidisk.items()
        if have.get(key) != want
    ]


def disk_diffs(spec: GuestSpec, live: dict[str, str]) -> list[Diff]:
    rows: list[Diff] = []
    missing = [disk for disk in spec.disks if disk.key not in live]
    for disk in spec.disks:
        if disk in missing:
            rows.append(_missing_disk(spec, disk, missing, live))
            continue
        have = parse_disk(spec.kind, disk.key, live[disk.key])
        rows += _disk_field_diffs(spec, disk, have)
    return rows


def _unused_on(disk: DiskSpec, live: dict[str, str]) -> list[str]:
    """`unusedN` keys on the disk's storage, in N order (unused10 after unused2).

    A bind mount has no storage and an unused volume always has one, so a missing
    bind mount is never offered one."""
    found = [
        key
        for key, value in live.items()
        if UNUSED_RE.match(key) and storage_of(volume_of(value)) == disk.storage
    ]
    return sorted(found, key=lambda key: int(key.removeprefix("unused")))


def _missing_disk(
    spec: GuestSpec, disk: DiskSpec, missing: list[DiskSpec], live: dict[str, str]
) -> Diff:
    """A spec disk with no key. After an in-place qmrestore (or a detach) its data
    may sit in an `unusedN` volume: point at that, never at a blank new disk."""
    unused = _unused_on(disk, live)
    if not unused:
        impact = LIVE if spec.kind == "qemu" else REBOOT
        return diff(disk.key, f"new {disk.role} disk", None, impact)
    rivals = [other for other in missing if other.storage == disk.storage]
    if len(unused) > 1 or len(rivals) > 1:
        # Which volume holds which disk's data is not knowable from the config.
        return diff(disk.key, f"reattach one of {', '.join(unused)}", None, CONFIG)
    (key,) = unused
    fix = command(reattach(spec, disk, volume_of(live[key])))
    return Diff(disk.key, f"reattach {key}", _show(None), CONFIG, fix)


def _disk_field_diffs(spec: GuestSpec, disk: DiskSpec, have: LiveDisk) -> list[Diff]:
    rows = _disk_size_diffs(disk, have)
    checks = [
        ("storage", disk.storage, have.storage, REFUSED),
        ("source", disk.source, have.source, REBOOT),
        ("backup", disk.backup, have.backup, CONFIG),
        ("replicate", disk.replicate, have.replicate, CONFIG),
        ("shared", disk.shared, have.shared, CONFIG),
    ]
    if spec.kind == "lxc":
        checks.append(("mount", disk.mount, have.mount, REBOOT))
    for option, want in spec.disk_opts.items():
        checks.append((option, want, have.opts.get(option), REBOOT))
    rows += [
        diff(f"{disk.key}.{name}", want, got, impact)
        for name, want, got, impact in checks
        if want != got
    ]
    return rows


def _disk_size_diffs(disk: DiskSpec, have: LiveDisk) -> list[Diff]:
    # No size in the spec means "no quota" on a CT mount point; leave it alone.
    if disk.size is None:
        return []
    if have.size is None or disk.size > have.size:
        impact = LIVE
    elif disk.size < have.size:
        impact = f"{REFUSED} (shrink)"
    else:
        return []
    return [diff(f"{disk.key}.size", format_size(disk.size), format_size(have.size), impact)]


def net_diffs(spec: GuestSpec, live: dict[str, str]) -> list[Diff]:
    rows: list[Diff] = []
    for net in spec.nets:
        if net.key not in live:
            rows.append(diff(net.key, f"new {net.name} {net.mac}", None, LIVE))
            continue
        rows += _net_field_diffs(spec, net, parse_net(spec.kind, live[net.key]))
    return rows


def _net_field_diffs(spec: GuestSpec, net: NetSpec, have: LiveNet) -> list[Diff]:
    checks = [
        ("mac", net.mac, have.mac, REFUSED),
        ("bridge", net.bridge, have.bridge, LIVE),
        ("tag", net.tag, have.tag, LIVE),
        ("firewall", net.firewall, have.firewall, LIVE),
    ]
    if spec.kind == "qemu":
        checks.append(("model", spec.net_model, have.model, REBOOT))
    else:
        checks += [
            ("name", net.name, have.name, REBOOT),
            ("ip", net.ip, have.ip, LIVE),
            ("gw", net.gw, have.gw, LIVE),
        ]
    return [
        diff(f"{net.key}.{name}", want, got, impact)
        for name, want, got, impact in checks
        if want != got
    ]


def unmanaged_diffs(spec: GuestSpec, live: dict[str, str]) -> list[Diff]:
    managed = set(spec.scalars) | {disk.key for disk in spec.disks} | {n.key for n in spec.nets}
    if spec.efidisk:
        managed.add("efidisk0")
    return [
        diff(key, None, value, UNMANAGED)
        for key, value in sorted(live.items())
        if key not in managed and key not in IDENTITY
    ]


def ha_diffs(spec: GuestSpec, have: LiveHa | None) -> list[Diff]:
    want = spec.ha
    if want is None and have is None:
        return []
    if want is None or have is None:
        return [diff("ha", _ha_text(want), _ha_text(have), CLUSTER)]
    return _ha_field_diffs(want, have)


def _ha_field_diffs(want, have: LiveHa) -> list[Diff]:
    rows = []
    placement = CLUSTER
    if have.rule_members:
        placement += f" (splits rule {have.rules[0]}, shared with {', '.join(have.rule_members)})"
    if want.nodes != have.nodes:
        rows.append(diff("ha.nodes", _nodes_text(want.nodes), _nodes_text(have.nodes), placement))
    if want.strict != have.strict:
        rows.append(diff("ha.strict", want.strict, have.strict, placement))
    if want.failback != have.failback:
        rows.append(diff("ha.failback", want.failback, have.failback, CLUSTER))
    if len(have.rules) > 1:
        rows.append(diff("ha.rules", "one node-affinity rule", ", ".join(have.rules), CLUSTER))
    return rows


def _nodes_text(nodes: dict[str, int]) -> str:
    if not nodes:
        return "no rule"
    return ",".join(f"{node}:{priority}" for node, priority in sorted(nodes.items()))


def _ha_text(ha) -> str | None:
    if ha is None:
        return None
    return f"nodes {_nodes_text(ha.nodes)}"


def replication_diffs(spec: GuestSpec, have: list[LiveReplication]) -> list[Diff]:
    want = spec.replication
    if want is None:
        return [diff("replication", None, f"{job.job} -> {job.target}", CLUSTER) for job in have]
    if len(have) != 1:
        jobs = ", ".join(job.job for job in have) or None
        return [diff("replication", f"one job -> {want.target}", jobs, CLUSTER)]
    job = have[0]
    checks = [
        ("replication.target", want.target, job.target),
        ("replication.schedule", want.schedule, job.schedule),
        ("replication.disabled", False, job.disabled),
    ]
    return [diff(key, w, h, CLUSTER) for key, w, h in checks if w != h]


def guest_drift(
    spec: GuestSpec,
    live: dict[str, str],
    ha: LiveHa | None,
    replication: list[LiveReplication],
) -> list[Diff]:
    return [
        *scalar_diffs(spec, live),
        *efidisk_diffs(spec, live),
        *disk_diffs(spec, live),
        *net_diffs(spec, live),
        *unmanaged_diffs(spec, live),
        *ha_diffs(spec, ha),
        *replication_diffs(spec, replication),
    ]


def format_diff(row: Diff) -> str:
    return f"{row.key:<22} want={row.want:<26} have={row.have:<26} {row.impact}"
