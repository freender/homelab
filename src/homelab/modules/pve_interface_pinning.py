from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import (
    FileSpec,
    HostArtifacts,
    connection_for_host,
    is_qemu_guest,
    normalize_bool,
    require_text,
    run_module_deploy,
    write_file_map,
)
from ..output import print_sub
from ..ssh import build_files, diff_many
from .pve_guest.spec import parse_nets

REMOTE_ROOT = "/tmp/homelab-pve-interface-pinning"
INSTALLER = "scripts/install.py"
INTERPRETER = "python3"
WOL_SERVICE = "homelab-interface-wol.service"
WOL_CONFIG = "interface-wol.conf"
# A VM's networking is owned here once cloud-init has named its NIC on first boot
# (homelab-ops#153/#170): the `.network` next to each `.link`, then cloud-init and
# netplan go. The packages are the ones the Debian cloud image installs; libnetplan1
# is left to apt-upgrade's autoremove.
CLOUD_PACKAGES = ("cloud-init", "netplan.io", "netplan-generator", "python3-netplan")
CLOUD_PATHS = (
    "/etc/cloud",
    "/var/lib/cloud",
    "/etc/netplan",
    "/etc/ssh/sshd_config.d/50-cloud-init.conf",
)
# Hand-placed on riven and helm before this module owned the `.network` (#126, #140).
RETIRED_NETWORK_FILES = ("/etc/systemd/network/10-lan.network",)
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
# `ubuntu` is the fleet's generic Debian-family type (the helm/riven VMs): plain
# systemd-networkd/udev, so the same `.link` files apply unchanged.
SUPPORTED_TYPES = ("pve", "ubuntu")


@dataclass(frozen=True)
class InterfacePin:
    name: str
    mac: str
    role: str
    wake_on_lan: bool


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        "pve-interface-pinning",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda supported_hosts, _hosts: validate(root, supported_hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    registry = default_registry(root)
    script = root / "pve-interface-pinning" / INSTALLER
    if not script.is_file():
        raise ValueError(f"missing installer: {script}")
    for host in hosts:
        if str(registry.get(host, "config.type")) not in SUPPORTED_TYPES:
            raise ValueError(
                f"pve-interface-pinning only supports {'/'.join(SUPPORTED_TYPES)} hosts: {host}"
            )
        if registry.has(host, "ubuntu-setup"):
            # ubuntu-setup pins its own primary NIC with a udev rule; two owners
            # of one name race at boot.
            raise ValueError(
                f"pve-interface-pinning conflicts with ubuntu-setup's NIC pin on {host}"
            )
        pins = normalize_interface_pins(registry, host)
        validate_postinstall_alignment(registry, host, pins)


def validate_postinstall_alignment(registry, host: str, pins: tuple[InterfacePin, ...]) -> None:
    """Guard against pve-interface-pinning and pve-postinstall drifting apart.

    /etc/network/interfaces is rendered by pve-postinstall from two independent
    hosts.conf keys (`pve-postinstall.interfaces.mgmt_iface`/`storage_iface`) that are
    never cross-checked against the interface names this module actually pins by MAC
    (`pve-interface-pinning.interfaces[].name`). If they disagree, the rendered
    /etc/network/interfaces references an interface name systemd-networkd never
    creates, and the host comes back from its next reboot unreachable. Both keys
    default to the same "nic0"/"nic1" strings, so this drift is otherwise invisible
    until an operator changes one side without the other.
    """
    try:
        interfaces_config = registry.get(host, "pve-postinstall.interfaces")
    except HostLookupError:
        return
    if not isinstance(interfaces_config, dict):
        return

    pins_by_role: dict[str, list[InterfacePin]] = {}
    for pin in pins:
        pins_by_role.setdefault(pin.role, []).append(pin)
    pinned_names = {pin.name for pin in pins}

    mgmt_iface = str(registry.get(host, "pve-postinstall.interfaces.mgmt_iface", "nic0"))
    _require_iface_pinned(host, "mgmt_iface", mgmt_iface, pinned_names)
    _require_role_matches_iface(host, "management", "mgmt_iface", mgmt_iface, pins_by_role)

    # storage_iface/corosync_iface only need to align when the host actually
    # declares the matching *_ip; a host without that NIC omits both and has
    # nothing to cross-check here.
    for kind, default_iface in (("storage", "nic1"), ("corosync", "nic2")):
        if registry.get(host, f"pve-postinstall.interfaces.{kind}_ip", None) is None:
            continue
        key = f"{kind}_iface"
        iface = str(registry.get(host, f"pve-postinstall.interfaces.{key}", default_iface))
        _require_iface_pinned(host, key, iface, pinned_names)
        _require_role_matches_iface(host, kind, key, iface, pins_by_role)


def _require_iface_pinned(host: str, key: str, iface: str, pinned_names: set[str]) -> None:
    if iface not in pinned_names:
        raise ValueError(
            f"pve-postinstall.interfaces.{key}={iface!r} for {host} has no matching "
            f"pve-interface-pinning.interfaces[].name; /etc/network/interfaces would "
            f"reference an interface systemd-networkd never creates"
        )


def _require_role_matches_iface(
    host: str,
    role: str,
    key: str,
    iface: str,
    pins_by_role: dict[str, list[InterfacePin]],
) -> None:
    role_pins = pins_by_role.get(role, [])
    if not role_pins:
        return
    role_names = {pin.name for pin in role_pins}
    if iface not in role_names:
        raise ValueError(
            f"pve-postinstall.interfaces.{key}={iface!r} for {host} does not match the "
            f"pve-interface-pinning.interfaces[] entry with role={role!r} "
            f"({sorted(role_names)}); interface roles have drifted from the pinned names"
        )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    artifacts = build_host_artifacts(root, host)
    connection = connection_for_host(root, host)

    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [
            (artifacts.build_dir / spec.build_name, spec.remote_path)
            for spec in artifacts.file_specs
        ],
    ):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(artifacts.build_dir):
            print_sub(f"    {file_name}")
        if is_qemu_guest(default_registry(root), host):
            print_sub(
                "[DRY-RUN] VM: once udev confirms every pin, would remove "
                f"{', '.join(RETIRED_NETWORK_FILES)} and purge whichever of "
                f"{', '.join(CLOUD_PACKAGES)} are installed"
            )
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-interface-pinning" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        interpreter=INTERPRETER,
        remote_subdirs=("build", "src"),
    )


def normalize_mac(value: object, message: str) -> str:
    mac = require_text(value, message).lower()
    if not MAC_RE.fullmatch(mac):
        raise ValueError(message)
    return mac


def normalize_interface_name(value: object, message: str) -> str:
    name = require_text(value, message)
    if not IFACE_RE.fullmatch(name):
        raise ValueError(message)
    return name


def normalize_interface_pins(registry, host: str) -> tuple[InterfacePin, ...]:
    if is_qemu_guest(registry, host):
        return vm_interface_pins(registry, host)
    raw = registry.get(host, "pve-interface-pinning.interfaces", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"pve-interface-pinning.interfaces must be a non-empty list for {host}")

    pins: list[InterfacePin] = []
    seen_names: set[str] = set()
    seen_macs: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"pve-interface-pinning.interfaces[{index}] must be a mapping for {host}"
            )
        name = normalize_interface_name(
            item.get("name", ""),
            f"pve-interface-pinning.interfaces[{index}].name invalid for {host}",
        )
        mac = normalize_mac(
            item.get("mac", ""),
            f"pve-interface-pinning.interfaces[{index}].mac invalid for {host}",
        )
        role = require_text(item.get("role", "ethernet"), "interface role is required")
        wake_on_lan = normalize_bool(
            item.get("wake_on_lan", False),
            False,
            f"pve-interface-pinning.interfaces[{index}].wake_on_lan must be boolean for {host}",
        )
        if name in seen_names:
            raise ValueError(f"duplicate pinned interface name for {host}: {name}")
        if mac in seen_macs:
            raise ValueError(f"duplicate pinned interface MAC for {host}: {mac}")
        seen_names.add(name)
        seen_macs.add(mac)
        pins.append(InterfacePin(name=name, mac=mac, role=role, wake_on_lan=wake_on_lan))
    return tuple(pins)


def vm_interface_pins(registry, host: str) -> tuple[InterfacePin, ...]:
    """A VM's pins are its `pve-guest.net` NICs: each one is declared once (#148).

    net0 keeps the `management` role the hand-written list gave it, so the
    rendered `.link` (and its comment) is unchanged; other NICs are named by key.
    """
    if registry.get(host, "pve-interface-pinning.interfaces", None) is not None:
        raise ValueError(
            f"{host} is a VM: declare its NICs in pve-guest.net, not "
            "pve-interface-pinning.interfaces (use `pve-interface-pinning: true`)"
        )
    nets = parse_nets(f"{host}.pve-guest", registry.get(host, "pve-guest.net", None), "qemu")
    return tuple(
        InterfacePin(
            name=net.name,
            mac=net.mac.lower(),
            role="management" if net.key == "net0" else net.key,
            wake_on_lan=False,
        )
        for net in nets
    )


def network_file(pin: InterfacePin, host: str) -> str:
    return "\n".join(
        [
            f"# Managed by homelab pve-interface-pinning for {host}: {pin.role}",
            "[Match]",
            f"Name={pin.name}",
            "",
            "[Network]",
            "DHCP=ipv4",
            "",
            "[DHCPv4]",
            "ClientIdentifier=mac",
            "",
        ]
    )


def link_file(pin: InterfacePin, host: str) -> str:
    lines = [
        f"# Managed by homelab pve-interface-pinning for {host}: {pin.role}",
        "[Match]",
        f"MACAddress={pin.mac}",
        "Type=ether",
        "",
        "[Link]",
        f"Name={pin.name}",
    ]
    if pin.wake_on_lan:
        lines.append("WakeOnLan=magic")
    return "\n".join(lines) + "\n"


def wol_script() -> str:
    return r'''#!/bin/bash

set -euo pipefail

CONFIG_FILE="/etc/homelab/interface-wol.conf"

print_sub() { echo "    $*"; }
print_warn() { echo "    Warning: $*" >&2; }

find_interface_by_mac() {
    local expected_mac="$1"
    local path
    local iface
    local current_mac

    for path in /sys/class/net/*; do
        [[ -e "$path/device" ]] || continue
        iface="${path##*/}"
        current_mac="$(<"$path/address")"
        if [[ "$current_mac" == "$expected_mac" ]]; then
            printf '%s\n' "$iface"
            return 0
        fi
    done
    return 1
}

if [[ ! -f "$CONFIG_FILE" ]]; then
    print_sub "No WOL interface config present; skipping"
    exit 0
fi

if ! command -v ethtool >/dev/null 2>&1; then
    print_warn "ethtool not found; cannot enable WOL"
    exit 0
fi

while IFS='|' read -r iface mac; do
    [[ -n "$iface" && "${iface:0:1}" != "#" ]] || continue
    if ! ip link show "$iface" >/dev/null 2>&1; then
        iface="$(find_interface_by_mac "$mac" || true)"
    fi
    if [[ -z "$iface" ]]; then
        print_warn "No interface found for WOL MAC $mac"
        continue
    fi
    if ethtool -s "$iface" wol g; then
        print_sub "Enabled WOL on $iface ($mac)"
    else
        print_warn "Failed to enable WOL on $iface ($mac)"
    fi
done < "$CONFIG_FILE"
'''


def wol_service() -> str:
    return """[Unit]
Description=Enable Wake-on-LAN on homelab pinned interfaces
After=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/homelab-interface-wol
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


def build_host_artifacts(root: Path, host: str) -> HostArtifacts:
    registry = default_registry(root)
    pins = normalize_interface_pins(registry, host)
    build_dir = root / "pve-interface-pinning" / "build" / host
    prepare_build_dir(build_dir)

    vm_guest = is_qemu_guest(registry, host)
    file_specs: list[FileSpec] = []
    for pin in pins:
        build_name = f"10-homelab-{pin.name}.link"
        (build_dir / build_name).write_text(link_file(pin, host), encoding="utf-8")
        file_specs.append(FileSpec(build_name, f"/etc/systemd/network/{build_name}"))
        if vm_guest:
            network_name = f"10-homelab-{pin.name}.network"
            (build_dir / network_name).write_text(network_file(pin, host), encoding="utf-8")
            file_specs.append(FileSpec(network_name, f"/etc/systemd/network/{network_name}"))

    wol_pins = [pin for pin in pins if pin.wake_on_lan]
    (build_dir / WOL_CONFIG).write_text(
        "".join(f"{pin.name}|{pin.mac}\n" for pin in wol_pins),
        encoding="utf-8",
    )
    (build_dir / "homelab-interface-wol").write_text(wol_script(), encoding="utf-8")
    (build_dir / WOL_SERVICE).write_text(
        wol_service(),
        encoding="utf-8",
    )

    file_specs.extend(
        [
            FileSpec(WOL_CONFIG, f"/etc/homelab/{WOL_CONFIG}", "644"),
            FileSpec("homelab-interface-wol", "/usr/local/sbin/homelab-interface-wol", "755"),
            FileSpec(WOL_SERVICE, f"/etc/systemd/system/{WOL_SERVICE}", "644"),
        ]
    )
    write_file_map(build_dir, tuple(file_specs))
    write_env_file(
        build_dir / "env",
        {
            "VM_GUEST": "true" if vm_guest else "false",
            "CLOUD_PACKAGES": " ".join(CLOUD_PACKAGES) if vm_guest else "",
            "CLOUD_PATHS": " ".join(CLOUD_PATHS) if vm_guest else "",
            "RETIRED_NETWORK_FILES": " ".join(RETIRED_NETWORK_FILES) if vm_guest else "",
        },
    )
    return HostArtifacts(build_dir=build_dir, file_specs=tuple(file_specs))
