from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, HostArtifacts, require_text, write_file_map
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-interface-pinning"
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


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
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-interface-pinning")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-interface-pinning (not applicable to {requested_host})")
        return 0

    validate(root, supported_hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    registry = default_registry(root)
    script = root / "pve-interface-pinning" / "scripts" / "install.sh"
    if not script.is_file():
        raise ValueError(f"missing installer: {script}")
    for host in hosts:
        if str(registry.get(host, "config.type")) != "pve":
            raise ValueError(f"pve-interface-pinning only supports PVE hosts: {host}")
        normalize_interface_pins(registry, host)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    artifacts = build_host_artifacts(root, host)
    connection = HostConnection(host)

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
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (artifacts.build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-interface-pinning" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def normalize_bool(value: object, default: bool, message: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(message)


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

    file_specs: list[FileSpec] = []
    link_names: list[str] = []
    for pin in pins:
        build_name = f"10-homelab-{pin.name}.link"
        (build_dir / build_name).write_text(link_file(pin, host), encoding="utf-8")
        file_specs.append(FileSpec(build_name, f"/etc/systemd/network/{build_name}"))
        link_names.append(build_name)

    wol_pins = [pin for pin in pins if pin.wake_on_lan]
    (build_dir / "interface-wol.conf").write_text(
        "".join(f"{pin.name}|{pin.mac}\n" for pin in wol_pins),
        encoding="utf-8",
    )
    (build_dir / "homelab-interface-wol").write_text(wol_script(), encoding="utf-8")
    (build_dir / "homelab-interface-wol.service").write_text(
        wol_service(),
        encoding="utf-8",
    )
    (build_dir / "link-files.conf").write_text(
        "".join(f"{name}\n" for name in link_names),
        encoding="utf-8",
    )

    file_specs.extend(
        [
            FileSpec("interface-wol.conf", "/etc/homelab/interface-wol.conf", "644"),
            FileSpec("homelab-interface-wol", "/usr/local/sbin/homelab-interface-wol", "755"),
            FileSpec(
                "homelab-interface-wol.service",
                "/etc/systemd/system/homelab-interface-wol.service",
                "644",
            ),
        ]
    )
    write_file_map(build_dir, tuple(file_specs))
    return HostArtifacts(build_dir=build_dir, file_specs=tuple(file_specs))
