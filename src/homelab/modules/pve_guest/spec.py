"""The `pve-guest` block: schema, profiles, roles, and per-guest validation.

A guest's Proxmox spec lives on its own hosts.conf entry (homelab-ops#148: HA moves
guests, so a list on a node goes stale). Shared settings come from a profile in this
file; any profile key can be overridden on the guest. Rare keys go under an
allowlisted `extra:`. Disks carry a `role` (homelab-ops#154) that sets backup and
mount defaults, and the guest-side disk module reads the same block.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .pveconf import is_disk_key, is_net_key, parse_size

FEATURE = "pve-guest"
KINDS = ("qemu", "lxc")
DEFAULT_STORAGE = "vm-flash"
DEFAULT_BRIDGE = "vmbr0"
DEFAULT_SCHEDULE = "*/15"


@dataclass(frozen=True)
class Profile:
    kind: str
    settings: dict[str, str]
    efidisk: dict[str, str]
    disk_opts: dict[str, str]
    net_model: str | None


PROFILES: dict[str, Profile] = {
    "linux-vm": Profile(
        kind="qemu",
        settings={
            "machine": "q35",
            "bios": "ovmf",
            "cpu": "x86-64-v3",
            "sockets": "1",
            "numa": "0",
            "scsihw": "virtio-scsi-single",
            "ostype": "l26",
            "agent": "enabled=1",
            "tablet": "0",
            "onboot": "1",
        },
        efidisk={"efitype": "4m", "pre-enrolled-keys": "0"},
        disk_opts={"discard": "on", "iothread": "1", "ssd": "1"},
        net_model="virtio",
    ),
    "docker-ct": Profile(
        kind="lxc",
        settings={
            "arch": "amd64",
            "ostype": "debian",
            "unprivileged": "1",
            "onboot": "1",
            "swap": "0",
            "features": "keyctl=1,nesting=1",
        },
        efidisk={},
        disk_opts={},
        net_model=None,
    ),
    "plain-ct": Profile(
        kind="lxc",
        settings={
            "arch": "amd64",
            "ostype": "debian",
            "unprivileged": "1",
            "onboot": "1",
            "swap": "0",
            "features": "nesting=1",
        },
        efidisk={},
        disk_opts={},
        net_model=None,
    ),
}


@dataclass(frozen=True)
class Role:
    backup: bool
    mount: str | None
    mount_required: bool = False


ROLES: dict[str, Role] = {
    "os": Role(backup=True, mount=None),
    "appdata": Role(backup=True, mount="/mnt/cache/appdata"),
    "scratch": Role(backup=False, mount="/mnt/scratch"),
    "data": Role(backup=True, mount=None, mount_required=True),
}

TYPED_KEYS = frozenset(
    {
        # The framework's host-level gate (hosts.py), not a guest setting.
        "deploy",
        "vmid",
        "kind",
        "home_node",
        "profile",
        "cores",
        "memory",
        "balloon",
        "disks",
        "net",
        "ha",
        "replication",
        "extra",
    }
)
EXTRA_KEY_RE = re.compile(
    r"^(dev\d+|hostpci\d+|usb\d+|serial\d+|startup|tpmstate0|affinity|vcpus|tags|hookscript|vga)$"
)
DISK_FIELDS = frozenset(
    {"role", "size", "storage", "backup", "replicate", "mount", "source", "shared"}
)
NET_FIELDS = frozenset({"name", "mac", "bridge", "tag", "firewall", "ip", "gw"})
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


@dataclass(frozen=True)
class DiskSpec:
    key: str
    role: str
    size: int | None
    storage: str | None
    source: str | None
    backup: bool
    replicate: bool
    mount: str | None
    shared: bool


@dataclass(frozen=True)
class NetSpec:
    key: str
    name: str
    mac: str
    bridge: str
    tag: int | None
    firewall: bool
    ip: str | None
    gw: str | None


@dataclass(frozen=True)
class HaSpec:
    nodes: dict[str, int]
    strict: bool
    failback: bool


@dataclass(frozen=True)
class ReplicationSpec:
    target: str
    schedule: str


@dataclass(frozen=True)
class GuestSpec:
    host: str
    vmid: int
    kind: str
    home_node: str
    profile: str
    scalars: dict[str, str]
    efidisk: dict[str, str]
    disk_opts: dict[str, str]
    net_model: str | None
    disks: tuple[DiskSpec, ...]
    nets: tuple[NetSpec, ...]
    ha: HaSpec | None
    replication: ReplicationSpec | None

    @property
    def name_key(self) -> str:
        return "name" if self.kind == "qemu" else "hostname"


def to_pve(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _mapping(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return value


def _positive_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{where} must be a positive integer")
    return value


def _flag(value: object, default: bool, where: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be true or false")
    return value


def _scalar(value: object, where: str) -> str:
    if isinstance(value, dict | list) or value is None:
        raise ValueError(f"{where} must be a scalar")
    return to_pve(value)


def _choice(value: object, choices, where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{where} must be one of {', '.join(sorted(choices))}, got {value!r}")
    return str(value)


def parse_guest(host: str, raw: object, nodes: dict[str, bool]) -> GuestSpec:
    """Validate one `pve-guest` block. `nodes` maps each PVE node to `standalone`."""
    where = f"{host}: {FEATURE}"
    raw = _mapping(raw, where)
    kind = _choice(raw.get("kind"), KINDS, f"{where}.kind")
    profile_name = _choice(raw.get("profile"), PROFILES, f"{where}.profile")
    profile = PROFILES[profile_name]
    if profile.kind != kind:
        raise ValueError(f"{where}.profile {profile_name} is for {profile.kind} guests")
    _check_keys(where, raw, profile)
    home_node = _choice(raw.get("home_node"), nodes, f"{where}.home_node")
    disks = parse_disks(where, raw.get("disks"), kind)
    ha = parse_ha(where, raw.get("ha"))
    replication = parse_replication(where, raw.get("replication"))
    check_placement(where, home_node, ha, replication, nodes)
    check_ha_disks(where, ha, disks)
    return GuestSpec(
        host=host,
        vmid=_positive_int(raw.get("vmid"), f"{where}.vmid"),
        kind=kind,
        home_node=home_node,
        profile=profile_name,
        scalars=build_scalars(host, where, raw, profile, disks),
        efidisk=dict(profile.efidisk),
        disk_opts=dict(profile.disk_opts),
        net_model=profile.net_model,
        disks=disks,
        nets=parse_nets(where, raw.get("net"), kind),
        ha=ha,
        replication=replication,
    )


def _check_keys(where: str, raw: dict, profile: Profile) -> None:
    unknown = sorted(set(raw) - TYPED_KEYS - set(profile.settings))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")


def build_scalars(
    host: str, where: str, raw: dict, profile: Profile, disks: tuple[DiskSpec, ...]
) -> dict[str, str]:
    """Every plain config key the guest should have, as the string PVE reports."""
    scalars = dict(profile.settings)
    for key in profile.settings:
        if key in raw:
            scalars[key] = _scalar(raw[key], f"{where}.{key}")
    memory = _positive_int(raw.get("memory"), f"{where}.memory")
    scalars["cores"] = str(_positive_int(raw.get("cores"), f"{where}.cores"))
    scalars["memory"] = str(memory)
    scalars["hostname" if profile.kind == "lxc" else "name"] = host
    if profile.kind == "qemu":
        scalars.update(_qemu_scalars(where, raw, memory, disks))
    elif "balloon" in raw:
        raise ValueError(f"{where}.balloon is a qemu-only key")
    scalars.update(parse_extra(where, raw.get("extra")))
    return scalars


def _qemu_scalars(
    where: str, raw: dict, memory: int, disks: tuple[DiskSpec, ...]
) -> dict[str, str]:
    os_disk = next(disk.key for disk in disks if disk.role == "os")
    scalars = {"boot": f"order={os_disk}"}
    if "balloon" in raw:
        balloon = raw["balloon"]
        if isinstance(balloon, bool) or not isinstance(balloon, int) or not 0 <= balloon <= memory:
            raise ValueError(f"{where}.balloon must be an integer from 0 to memory")
        scalars["balloon"] = str(balloon)
    return scalars


def parse_extra(where: str, raw: object) -> dict[str, str]:
    """Rare keys. The allowlist shares no key with a profile or a typed field."""
    if raw is None:
        return {}
    extra: dict[str, str] = {}
    for key, value in _mapping(raw, f"{where}.extra").items():
        if not EXTRA_KEY_RE.match(str(key)):
            raise ValueError(f"{where}.extra.{key} is not an allowlisted rare key")
        extra[str(key)] = _scalar(value, f"{where}.extra.{key}")
    return extra


def parse_disks(where: str, raw: object, kind: str) -> tuple[DiskSpec, ...]:
    entries = _mapping(raw, f"{where}.disks")
    disks = tuple(
        parse_disk(f"{where}.disks.{key}", str(key), value, kind) for key, value in entries.items()
    )
    _check_os_disk(where, disks, kind)
    _check_unique_mounts(where, disks)
    return disks


def _check_os_disk(where: str, disks: tuple[DiskSpec, ...], kind: str) -> None:
    roots = [disk.key for disk in disks if disk.role == "os"]
    if len(roots) != 1:
        raise ValueError(f"{where}.disks needs exactly one role: os disk, found {roots}")
    if kind == "lxc" and roots != ["rootfs"]:
        raise ValueError(f"{where}.disks: the lxc os disk is rootfs")


def _check_unique_mounts(where: str, disks: tuple[DiskSpec, ...]) -> None:
    mounts = [disk.mount for disk in disks if disk.mount]
    duplicates = sorted({mount for mount in mounts if mounts.count(mount) > 1})
    if duplicates:
        raise ValueError(f"{where}.disks: mount path used twice: {', '.join(duplicates)}")


def parse_disk(where: str, key: str, raw: object, kind: str) -> DiskSpec:
    raw = _mapping(raw, where)
    if not is_disk_key(kind, key):
        raise ValueError(f"{where}: not a {kind} disk key")
    unknown = sorted(set(raw) - DISK_FIELDS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}")
    role_name = _choice(raw.get("role"), ROLES, f"{where}.role")
    role = ROLES[role_name]
    source = _bind_source(where, raw, kind)
    return DiskSpec(
        key=str(key),
        role=role_name,
        size=_disk_size(where, raw, kind, key),
        storage=None if source else str(raw.get("storage", DEFAULT_STORAGE)),
        source=source,
        backup=_flag(raw.get("backup"), role.backup, f"{where}.backup"),
        replicate=_flag(raw.get("replicate"), True, f"{where}.replicate"),
        mount=_disk_mount(where, raw, role),
        shared=_flag(raw.get("shared"), False, f"{where}.shared"),
    )


def _bind_source(where: str, raw: dict, kind: str) -> str | None:
    source = raw.get("source")
    if source is None:
        if "shared" in raw:
            raise ValueError(f"{where}.shared only applies to a host bind mount (source)")
        return None
    if kind != "lxc" or not str(source).startswith("/"):
        raise ValueError(f"{where}.source must be an absolute host path on an lxc mpN")
    if "size" in raw or "storage" in raw:
        raise ValueError(f"{where}: a host bind mount takes no size or storage")
    return str(source)


def _disk_size(where: str, raw: dict, kind: str, key: str) -> int | None:
    try:
        size = parse_size(raw["size"]) if "size" in raw else None
    except ValueError as exc:
        raise ValueError(f"{where}.size: {exc}") from None
    # A subvol mount point may run without a quota; a block device never can.
    if size is None and (kind == "qemu" or key == "rootfs"):
        raise ValueError(f"{where}.size is required")
    return size


def _disk_mount(where: str, raw: dict, role: Role) -> str | None:
    if role.mount is None and not role.mount_required:
        if "mount" in raw:
            raise ValueError(f"{where}: an os disk has no mount")
        return None
    mount = raw.get("mount", role.mount)
    if not mount or not str(mount).startswith("/"):
        raise ValueError(f"{where}.mount must be an absolute path")
    return str(mount)


def parse_nets(where: str, raw: object, kind: str) -> tuple[NetSpec, ...]:
    entries = _mapping(raw, f"{where}.net")
    if not entries:
        raise ValueError(f"{where}.net needs at least one NIC")
    return tuple(
        parse_net(f"{where}.net.{key}", str(key), value, kind) for key, value in entries.items()
    )


def parse_net(where: str, key: str, raw: object, kind: str) -> NetSpec:
    raw = _mapping(raw, where)
    if not is_net_key(key):
        raise ValueError(f"{where}: not a netN key")
    unknown = sorted(set(raw) - NET_FIELDS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {', '.join(unknown)}")
    if raw.get("name") != f"nic{key[3:]}":
        raise ValueError(f"{where}.name must be nic{key[3:]}")
    if not MAC_RE.fullmatch(str(raw.get("mac"))):
        raise ValueError(f"{where}.mac must be an upper-case MAC")
    ip, gw = _net_address(where, raw, kind)
    return NetSpec(
        key=key,
        name=str(raw["name"]),
        mac=str(raw["mac"]),
        bridge=str(raw.get("bridge", DEFAULT_BRIDGE)),
        tag=_vlan_tag(where, raw.get("tag")),
        firewall=_flag(raw.get("firewall"), True, f"{where}.firewall"),
        ip=ip,
        gw=gw,
    )


def _vlan_tag(where: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4094:
        raise ValueError(f"{where}.tag must be a VLAN id from 1 to 4094 (omit when untagged)")
    return value


def _net_address(where: str, raw: dict, kind: str) -> tuple[str | None, str | None]:
    if kind == "qemu":
        if "ip" in raw or "gw" in raw:
            raise ValueError(f"{where}: ip/gw are lxc-only (a VM configures its own address)")
        return None, None
    ip = str(raw.get("ip", "dhcp"))
    gw = raw.get("gw")
    if ip != "dhcp":
        _parse_ip(where, ip, "ip", ipaddress.ip_interface)
    if gw is not None:
        if ip == "dhcp":
            raise ValueError(f"{where}.gw needs a static ip")
        _parse_ip(where, str(gw), "gw", ipaddress.ip_address)
    return ip, None if gw is None else str(gw)


def _parse_ip(where: str, value: str, field_name: str, parser) -> None:
    try:
        parser(value)
    except ValueError as exc:
        raise ValueError(f"{where}.{field_name}: {exc}") from exc
    if field_name == "ip" and "/" not in value:
        raise ValueError(f"{where}.ip needs a prefix length (a.b.c.d/nn)")


def parse_ha(where: str, raw: object) -> HaSpec | None:
    if raw is None:
        return None
    raw = _mapping(raw, f"{where}.ha")
    unknown = sorted(set(raw) - {"nodes", "strict", "failback"})
    if unknown:
        raise ValueError(f"{where}.ha: unknown field(s) {', '.join(unknown)}")
    nodes = _mapping(raw.get("nodes"), f"{where}.ha.nodes")
    if not nodes:
        raise ValueError(f"{where}.ha.nodes must name at least one node")
    for node, priority in nodes.items():
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ValueError(f"{where}.ha.nodes.{node} must be a priority >= 0")
    return HaSpec(
        nodes={str(node): int(priority) for node, priority in nodes.items()},
        strict=_flag(raw.get("strict"), True, f"{where}.ha.strict"),
        failback=_flag(raw.get("failback"), True, f"{where}.ha.failback"),
    )


def parse_replication(where: str, raw: object) -> ReplicationSpec | None:
    if raw is None:
        return None
    raw = _mapping(raw, f"{where}.replication")
    unknown = sorted(set(raw) - {"target", "schedule"})
    if unknown or not raw.get("target"):
        raise ValueError(f"{where}.replication takes a target and an optional schedule")
    return ReplicationSpec(
        target=str(raw["target"]), schedule=str(raw.get("schedule", DEFAULT_SCHEDULE))
    )


def check_placement(
    where: str,
    home_node: str,
    ha: HaSpec | None,
    replication: ReplicationSpec | None,
    nodes: dict[str, bool],
) -> None:
    """HA and replication only exist inside the cluster, and need each other."""
    cluster = {node for node, standalone in nodes.items() if not standalone}
    if nodes[home_node] and (ha or replication):
        raise ValueError(f"{where}: standalone node {home_node} has no HA or replication")
    if replication is not None and (
        replication.target not in cluster or replication.target == home_node
    ):
        raise ValueError(f"{where}.replication.target must be another cluster node")
    if ha is not None:
        _check_ha_placement(where, home_node, ha, replication, cluster)


def _check_ha_placement(
    where: str,
    home_node: str,
    ha: HaSpec,
    replication: ReplicationSpec | None,
    cluster: set[str],
) -> None:
    if replication is None:
        raise ValueError(f"{where}: an HA guest needs replication (its disks are node-local)")
    if not set(ha.nodes) <= cluster or home_node not in ha.nodes:
        raise ValueError(f"{where}.ha.nodes must be cluster nodes including {home_node}")
    if replication.target not in ha.nodes:
        raise ValueError(f"{where}.replication.target must be one of ha.nodes")


def check_ha_disks(where: str, ha: HaSpec | None, disks: tuple[DiskSpec, ...]) -> None:
    if ha is None:
        return
    for disk in disks:
        if not disk.replicate:
            raise ValueError(f"{where}.disks.{disk.key}: an HA guest must replicate every disk")
        if disk.source and not disk.shared:
            raise ValueError(
                f"{where}.disks.{disk.key}: an HA guest's host bind mount must set shared: true"
            )
