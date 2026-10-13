"""Parse what Proxmox reports about a guest into comparable values.

Pure functions over `pvesh ... --output-format json` output: no SSH, no inventory.
Everything here exists so two configs that mean the same thing compare equal --
`agent: 1` and `agent: enabled=1`, `size=0T` and no quota, option order inside a
property string, MAC case, a missing key and its PVE default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SIZE_UNITS = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)([KMGT]?)")

QEMU_DISK_RE = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
LXC_DISK_RE = re.compile(r"^(rootfs|mp\d+)$")
NET_RE = re.compile(r"^net(\d+)$")
QEMU_NET_MODELS = ("virtio", "e1000", "e1000e", "vmxnet3", "rtl8139")
DISK_OPTS = ("discard", "iothread", "ssd")

# Option-list values whose order carries no meaning.
KV_KEYS = frozenset({"agent", "features", "startup", "boot", "efidisk0"})
KV_KEY_RE = re.compile(r"^(dev|hostpci|usb)\d+$")


def parse_size(value: object) -> int | None:
    """`32G` -> bytes. Zero means "no quota" on a subvol, so it maps to None."""
    if value is None:
        return None
    match = SIZE_RE.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"invalid size {value!r}")
    number = float(match.group(1)) * SIZE_UNITS.get(match.group(2), 1)
    return int(number) or None


def format_size(value: int | None) -> str:
    if value is None:
        return "none"
    for unit in "TGMK":
        if value % SIZE_UNITS[unit] == 0:
            return f"{value // SIZE_UNITS[unit]}{unit}"
    return str(value)


def to_bool(value: object) -> bool:
    """PVE writes every boolean as 1/0 (an int in JSON); an absent key arrives as None."""
    return str(value) == "1"


def split_prop(value: str, first_key: str | None = None) -> dict[str, str]:
    """`vm-flash:vm-1,size=32G` -> {first_key: 'vm-flash:vm-1', 'size': '32G'}."""
    parts: dict[str, str] = {}
    for index, part in enumerate(value.split(",")):
        name, sep, item = part.partition("=")
        if sep:
            parts[name] = item
        elif index == 0 and first_key:
            parts[first_key] = part
    return parts


def kv_form(value: str) -> dict[str, str]:
    """Order-insensitive form of an option list; `agent: 1` == `agent: enabled=1`."""
    if value in {"0", "1"}:
        value = f"enabled={value}"
    parts: dict[str, str] = {}
    for part in value.split(","):
        name, sep, item = part.partition("=")
        if sep:
            parts[name] = item
        elif part:
            parts["_"] = part
    return parts


def same_value(key: str, want: str, have: str | None) -> bool:
    if have is None:
        return False
    if key in KV_KEYS or KV_KEY_RE.match(key):
        return kv_form(want) == kv_form(have)
    return want == have


def is_disk_key(kind: str, key: str) -> bool:
    pattern = QEMU_DISK_RE if kind == "qemu" else LXC_DISK_RE
    return bool(pattern.match(key))


def is_net_key(key: str) -> bool:
    return bool(NET_RE.match(key))


@dataclass(frozen=True)
class LiveDisk:
    storage: str | None
    source: str | None
    size: int | None
    backup: bool
    replicate: bool
    mount: str | None
    shared: bool
    opts: dict[str, str] = field(default_factory=dict)


def parse_disk(kind: str, key: str, raw: str) -> LiveDisk:
    parts = split_prop(raw, "volume")
    volume = parts.get("volume", "")
    bind = volume.startswith("/")
    # PVE defaults: CT mount points are excluded from backup unless asked;
    # everything else is included.
    backup_default = not (kind == "lxc" and key != "rootfs")
    return LiveDisk(
        storage=None if bind else volume.partition(":")[0] or None,
        source=volume if bind else None,
        size=parse_size(parts.get("size")),
        backup=to_bool(parts["backup"]) if "backup" in parts else backup_default,
        replicate=to_bool(parts.get("replicate", "1")),
        mount=parts.get("mp"),
        shared=to_bool(parts.get("shared")),
        opts={name: parts[name] for name in DISK_OPTS if name in parts},
    )


@dataclass(frozen=True)
class LiveNet:
    mac: str | None
    model: str | None
    name: str | None
    bridge: str
    tag: int | None
    firewall: bool
    ip: str | None
    gw: str | None


def parse_net(kind: str, raw: str) -> LiveNet:
    parts = split_prop(raw)
    model = None
    mac = parts.get("hwaddr")
    if kind == "qemu":
        model = next((name for name in QEMU_NET_MODELS if name in parts), None)
        mac = parts.get(model)
    return LiveNet(
        mac=mac.upper() if mac else None,
        model=model,
        name=parts.get("name"),
        bridge=parts.get("bridge", "vmbr0"),
        tag=int(parts["tag"]) if "tag" in parts else None,
        firewall=to_bool(parts.get("firewall")),
        ip=parts.get("ip"),
        gw=parts.get("gw"),
    )


@dataclass(frozen=True)
class PendingChange:
    key: str
    current: str | None
    pending: str | None
    delete: bool


def parse_pending(entries: list[dict]) -> tuple[dict[str, str], list[PendingChange]]:
    """Split `/nodes/<n>/<kind>/<vmid>/pending` into current config and pending changes.

    The pending endpoint is one call that returns both: `value` is what the guest
    runs with now, `pending`/`delete` are queued for its next restart. Drift is
    measured against the current value only.
    """
    current: dict[str, str] = {}
    pending: list[PendingChange] = []
    for entry in entries:
        key = str(entry["key"])
        value = str(entry["value"]) if "value" in entry else None
        if value is not None:
            current[key] = value
        if "pending" in entry or entry.get("delete"):
            new = str(entry["pending"]) if "pending" in entry else None
            pending.append(PendingChange(key, value, new, bool(entry.get("delete"))))
    return current, pending


@dataclass(frozen=True)
class LiveHa:
    nodes: dict[str, int]
    strict: bool
    failback: bool
    rules: tuple[str, ...]
    rule_members: tuple[str, ...]


def ha_sid(kind: str, vmid: int) -> str:
    return f"{'vm' if kind == 'qemu' else 'ct'}:{vmid}"


def parse_ha_nodes(value: str) -> dict[str, int]:
    nodes: dict[str, int] = {}
    for item in value.split(","):
        name, _, priority = item.partition(":")
        nodes[name] = int(priority or 0)
    return nodes


def parse_ha(resources: list[dict], rules: list[dict]) -> dict[str, LiveHa]:
    """HA state per resource id (`vm:112`, `ct:108`).

    Only `node-affinity` rules carry placement. A guest listed in more than one is
    reported with every rule name so the drift report can flag it.
    """
    affinity = [rule for rule in rules if rule.get("type") == "node-affinity"]
    by_sid: dict[str, LiveHa] = {}
    for resource in resources:
        sid = str(resource["sid"])
        mine = [rule for rule in affinity if sid in _members(rule)]
        by_sid[sid] = _live_ha(sid, resource, mine)
    return by_sid


def _members(rule: dict) -> list[str]:
    return str(rule["resources"]).split(",")


def _live_ha(sid: str, resource: dict, mine: list[dict]) -> LiveHa:
    if not mine:
        return LiveHa({}, False, to_bool(resource.get("failback", 1)), (), ())
    first = mine[0]
    return LiveHa(
        nodes=parse_ha_nodes(str(first["nodes"])),
        strict=to_bool(first.get("strict")),
        failback=to_bool(resource.get("failback", 1)),
        rules=tuple(str(rule["rule"]) for rule in mine),
        rule_members=tuple(member for member in _members(first) if member != sid),
    )


@dataclass(frozen=True)
class LiveReplication:
    job: str
    target: str
    schedule: str
    disabled: bool


def parse_replication(jobs: list[dict]) -> dict[int, list[LiveReplication]]:
    by_guest: dict[int, list[LiveReplication]] = {}
    for job in jobs:
        by_guest.setdefault(int(job["guest"]), []).append(
            LiveReplication(
                job=str(job["id"]),
                target=str(job["target"]),
                schedule=str(job.get("schedule", "*/15")),
                disabled=to_bool(job.get("disable")),
            )
        )
    return by_guest
