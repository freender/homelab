"""What creating a guest runs, worked out from its spec (pure).

Every command line and cloud-init document `homelab guest create` sends to the
home node comes from here, so a wrong argument shows up in a test rather than as
a guest with the wrong shape. Running them, in order and with cleanup, is
`create.py`.

VMs boot the Debian generic cloud image, imported fresh into the `os` disk, with
two cloud-init snippets: root's key, hostname and the guest agent; and each NIC
named and matched by MAC on DHCP (homelab-ops#153). There is no default user.
LXCs come from the newest `debian-13-standard` template. Everything past "boots
and takes root's key" is L3, applied by `./deploy all <host>`.
"""

from __future__ import annotations

import base64
import re
import shlex

import yaml

from .pveconf import SIZE_UNITS
from .spec import FEATURE, DiskSpec, GuestSpec, NetSpec

IMAGE_BASE = "https://cloud.debian.org/images/cloud/trixie/latest"
IMAGE_NAME = "debian-13-generic-amd64.qcow2"
SUMS_URL = f"{IMAGE_BASE}/SHA512SUMS"
LOCAL_DIR = "/var/lib/vz"
TEMPLATE_RE = re.compile(r"^debian-13-standard_(\d+(?:\.\d+)*)-(\d+)_amd64\.tar\.zst$")
SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
GIB = SIZE_UNITS["G"]


def image_file(vmid: int) -> str:
    """The downloaded image, named per guest so two creates never share one."""
    return f"homelab-{vmid}-{IMAGE_NAME}"


def snippet_files(vmid: int) -> tuple[str, str]:
    return f"homelab-{vmid}-user.yaml", f"homelab-{vmid}-network.yaml"


def key_file(vmid: int) -> str:
    return f"/tmp/homelab-pve-guest-{vmid}.pub"


def scratch_paths(spec: GuestSpec) -> list[str]:
    """Every file a create leaves on the home node, removed when it ends."""
    if spec.kind == "lxc":
        return [key_file(spec.vmid)]
    user, network = snippet_files(spec.vmid)
    return [
        f"{LOCAL_DIR}/import/{image_file(spec.vmid)}",
        f"{LOCAL_DIR}/snippets/{user}",
        f"{LOCAL_DIR}/snippets/{network}",
    ]


def image_checksum(sums: str) -> str:
    """The SHA512 for the generic amd64 image, from Debian's SHA512SUMS."""
    for line in sums.splitlines():
        digest, _, name = line.strip().partition("  ")
        if name == IMAGE_NAME:
            if not SHA512_RE.fullmatch(digest):
                raise ValueError(f"SHA512SUMS: malformed digest for {IMAGE_NAME}")
            return digest
    raise ValueError(f"SHA512SUMS lists no {IMAGE_NAME}")


def newest_template(available: str) -> str:
    """The newest amd64 debian-13-standard in `pveam available --section system`."""
    found: list[tuple[tuple[int, ...], str]] = []
    # Lines are `<section> <template> <arch>`; the template is the second field.
    for line in available.splitlines():
        fields = line.split()
        match = TEMPLATE_RE.fullmatch(fields[1]) if len(fields) > 1 else None
        if match:
            version = (*map(int, match.group(1).split(".")), int(match.group(2)))
            found.append((version, match.group(0)))
    if not found:
        raise ValueError("pveam lists no amd64 debian-13-standard template")
    return max(found)[1]


def gib(spec: GuestSpec, disk: DiskSpec) -> int:
    """A disk's size in whole GiB, which is all `qm`/`pct` allocate in.

    A CT mount point without a size has no quota; ZFS takes 0 for that.
    """
    if disk.size is None:
        return 0
    if disk.size % GIB:
        raise ValueError(
            f"{spec.host}: {FEATURE}.disks.{disk.key}.size must be whole GiB to create it"
        )
    return disk.size // GIB


def user_data(spec: GuestSpec, root_key: str, fqdn: str) -> str:
    document = {
        "hostname": spec.host,
        "fqdn": fqdn,
        "manage_etc_hosts": True,
        "users": [],
        "disable_root": False,
        "ssh_authorized_keys": [root_key],
        "package_update": True,
        "packages": ["qemu-guest-agent"],
        # The package's unit starts on the virtio port's udev event, which has
        # already fired by the time cloud-init installs it.
        "runcmd": [["systemctl", "start", "qemu-guest-agent"]],
    }
    return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)


def network_config(spec: GuestSpec) -> str:
    ethernets = {
        net.name: {
            "match": {"macaddress": net.mac.lower()},
            "set-name": net.name,
            "dhcp4": True,
            "dhcp-identifier": "mac",
        }
        for net in spec.nets
    }
    return yaml.safe_dump({"network": {"version": 2, "ethernets": ethernets}}, sort_keys=False)


def _prop(head: str, items: list[tuple[str, object]]) -> str:
    """`head,key=value,...`, skipping every None value."""
    return ",".join([head, *(f"{key}={value}" for key, value in items if value is not None)])


def _flag(value: bool, default: bool) -> str | None:
    """`0`/`1` only where it differs from what PVE assumes when the key is absent."""
    return None if value == default else ("1" if value else "0")


def vm_disk(spec: GuestSpec, disk: DiskSpec, image_volid: str) -> str:
    if disk.role == "os":
        volume = f"{disk.storage}:0,import-from={image_volid}"
    else:
        volume = f"{disk.storage}:{gib(spec, disk)}"
    return _prop(
        volume,
        [
            ("backup", _flag(disk.backup, True)),
            ("replicate", _flag(disk.replicate, True)),
            *spec.disk_opts.items(),
        ],
    )


def vm_net(spec: GuestSpec, net: NetSpec) -> str:
    return _prop(
        f"{spec.net_model}={net.mac}",
        [("bridge", net.bridge), ("firewall", "1" if net.firewall else "0"), ("tag", net.tag)],
    )


def _os_disk(spec: GuestSpec) -> DiskSpec:
    return next(disk for disk in spec.disks if disk.role == "os")


def _scalar_args(spec: GuestSpec) -> list[str]:
    args: list[str] = []
    for key, value in spec.scalars.items():
        args += [f"--{key}", value]
    return args


def vm_create(spec: GuestSpec, image_volid: str) -> list[str]:
    os_disk = _os_disk(spec)
    user, network = snippet_files(spec.vmid)
    args = ["qm", "create", str(spec.vmid), *_scalar_args(spec)]
    args += ["--efidisk0", _prop(f"{os_disk.storage}:1", list(spec.efidisk.items()))]
    for disk in spec.disks:
        args += [f"--{disk.key}", vm_disk(spec, disk, image_volid)]
    for net in spec.nets:
        args += [f"--{net.key}", vm_net(spec, net)]
    args += [
        "--ide2",
        "local:cloudinit",
        "--cicustom",
        f"user=local:snippets/{user},network=local:snippets/{network}",
    ]
    return args


def vm_resize(spec: GuestSpec) -> list[str]:
    os_disk = _os_disk(spec)
    return ["qm", "disk", "resize", str(spec.vmid), os_disk.key, f"{gib(spec, os_disk)}G"]


def lxc_disk(spec: GuestSpec, disk: DiskSpec) -> str:
    volume = disk.source or f"{disk.storage}:{gib(spec, disk)}"
    # A CT mount point is left out of backups unless it says backup=1.
    return _prop(
        volume,
        [
            ("mp", disk.mount),
            ("backup", _flag(disk.backup, disk.key == "rootfs")),
            ("replicate", _flag(disk.replicate, True)),
            ("shared", "1" if disk.shared else None),
        ],
    )


def lxc_net(net: NetSpec) -> str:
    return _prop(
        f"name={net.name}",
        [
            ("bridge", net.bridge),
            ("firewall", "1" if net.firewall else "0"),
            ("hwaddr", net.mac),
            ("ip", net.ip),
            ("gw", net.gw),
            ("tag", net.tag),
        ],
    )


def lxc_create(spec: GuestSpec, template_volid: str) -> list[str]:
    args = ["pct", "create", str(spec.vmid), template_volid, *_scalar_args(spec)]
    args += ["--ssh-public-keys", key_file(spec.vmid)]
    for disk in spec.disks:
        args += [f"--{disk.key}", lxc_disk(spec, disk)]
    for net in spec.nets:
        args += [f"--{net.key}", lxc_net(net)]
    return args


def download(spec: GuestSpec, digest: str) -> list[str]:
    return [
        "pvesh",
        "create",
        f"/nodes/{spec.home_node}/storage/local/download-url",
        "--content",
        "import",
        "--filename",
        image_file(spec.vmid),
        "--url",
        f"{IMAGE_BASE}/{IMAGE_NAME}",
        "--checksum",
        digest,
        "--checksum-algorithm",
        "sha512",
    ]


def write_file(path: str, content: str) -> str:
    """A shell command that writes `content` to `path`, whatever bytes it holds."""
    encoded = base64.b64encode(content.encode()).decode()
    return f"echo {encoded} | base64 -d > {shlex.quote(path)}"


def command(argv: list[str]) -> str:
    return shlex.join(argv)
