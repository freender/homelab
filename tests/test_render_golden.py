"""Golden-file render tests for the modules that can take a node off the network.

`pve-postinstall` (writes /etc/network/interfaces), `pve-interface-pinning` (renames
NICs by MAC), `pve-gpu-passthrough` (rewrites the boot cmdline) and `pve-autoinstall`
(writes unattended-installer answer files) had zero test coverage. A bad render in any
of them is only discovered after a reboot, on a host you can no longer reach.

These render against the real hosts.conf, so they also catch inventory drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from homelab.deploy import prepare_build_dir
from homelab.hosts import default_registry
from homelab.modules import (
    pve_autoinstall,
    pve_gpu_passthrough,
    pve_interface_pinning,
    pve_postinstall,
)

ROOT = Path(__file__).resolve().parents[1]

# A rendered artifact must never contain an unsubstituted Jinja placeholder.
UNRENDERED = re.compile(r"\{\{|\}\}|\{%")


def _hosts_with(feature: str) -> list[str]:
    return default_registry(ROOT).list_hosts(feature=feature)


# --------------------------------------------------------------------------------------
# pve-postinstall: /etc/network/interfaces
# --------------------------------------------------------------------------------------


def _interface_hosts() -> list[str]:
    registry = default_registry(ROOT)
    hosts = []
    for host in _hosts_with("pve-postinstall"):
        config = registry.get(host, "pve-postinstall.interfaces", None)
        if isinstance(config, dict):
            hosts.append(host)
    return hosts


def test_there_are_interface_hosts_to_cover() -> None:
    # Guard the guard: if inventory stops declaring interfaces, the tests below would
    # silently pass by iterating nothing.
    assert _interface_hosts(), "expected at least one host with pve-postinstall.interfaces"


@pytest.mark.parametrize("host", _interface_hosts())
def test_interfaces_render_is_complete_and_addressed(host: str, tmp_path: Path) -> None:
    registry = default_registry(ROOT)
    build_dir = tmp_path / host
    prepare_build_dir(build_dir)

    pve_postinstall.build_network_interfaces_bundle(ROOT, host, build_dir)

    rendered = (build_dir / "interfaces").read_text(encoding="utf-8")

    assert not UNRENDERED.search(rendered), f"{host}: unrendered placeholder in interfaces"

    mgmt_ip = str(registry.get(host, "pve-postinstall.interfaces.mgmt_ip"))
    gateway = str(registry.get(host, "pve-postinstall.interfaces.gateway"))
    storage_ip = registry.get(host, "pve-postinstall.interfaces.storage_ip", None)

    # The management address and gateway are what we come back on after a reboot.
    assert mgmt_ip in rendered, f"{host}: mgmt_ip {mgmt_ip} missing from rendered interfaces"
    assert gateway in rendered, f"{host}: gateway {gateway} missing from rendered interfaces"
    # storage_ip is optional: a host with no dedicated storage NIC omits it and
    # renders management-only, with no vmbr1 stanza at all.
    if storage_ip is not None:
        assert str(storage_ip) in rendered, f"{host}: storage_ip {storage_ip} missing"
        assert re.search(r"^\s*auto vmbr1\b", rendered, re.MULTILINE), f"{host}: no vmbr1 iface"
    else:
        assert not re.search(
            r"^\s*auto vmbr1\b", rendered, re.MULTILINE
        ), f"{host}: unexpected vmbr1 iface with no storage_ip configured"

    # A loopback stanza and the mgmt bridge must exist, or the node boots unreachable.
    assert re.search(r"^\s*auto lo\b", rendered, re.MULTILINE), f"{host}: no loopback stanza"
    assert re.search(r"^\s*iface\s+vmbr0\b", rendered, re.MULTILINE), f"{host}: no vmbr0 iface"


# --------------------------------------------------------------------------------------
# pve-interface-pinning: systemd .link files (renames NICs by MAC)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", _hosts_with("pve-interface-pinning"))
def test_link_files_match_on_mac_and_set_expected_name(host: str) -> None:
    registry = default_registry(ROOT)
    pins = pve_interface_pinning.normalize_interface_pins(registry, host)
    assert pins, f"{host}: pve-interface-pinning enabled but no pins declared"

    seen_names: set[str] = set()
    seen_macs: set[str] = set()

    for pin in pins:
        rendered = pve_interface_pinning.link_file(pin, host)

        assert "[Match]" in rendered and "[Link]" in rendered
        assert f"MACAddress={pin.mac}" in rendered
        assert f"Name={pin.name}" in rendered
        # WakeOnLan must appear only when the pin asks for it.
        assert ("WakeOnLan=magic" in rendered) is bool(pin.wake_on_lan)
        assert not UNRENDERED.search(rendered)

        # A MAC is normalized to lowercase colon form; a stray uppercase or dashed MAC
        # silently fails to match and the NIC keeps its kernel name.
        assert re.fullmatch(
            r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", pin.mac
        ), f"{host}: MAC not normalized: {pin.mac!r}"

        # Two link files claiming the same name (or the same MAC) is a rename collision.
        assert pin.name not in seen_names, f"{host}: duplicate pinned name {pin.name}"
        assert pin.mac not in seen_macs, f"{host}: duplicate pinned MAC {pin.mac}"
        seen_names.add(pin.name)
        seen_macs.add(pin.mac)


# --------------------------------------------------------------------------------------
# pve-interface-pinning <-> pve-postinstall: mgmt_iface/storage_iface drift guard
#
# /etc/network/interfaces (pve-postinstall) and the .link files above (pve-interface-
# pinning) are driven by two independent hosts.conf keys that happen to share the same
# "nic0"/"nic1" defaults. Nothing else ties them together, so an operator changing one
# without the other silently renders /etc/network/interfaces against an interface name
# systemd-networkd never creates -- only discovered on the next reboot.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", _interface_hosts())
def test_postinstall_iface_names_match_pinned_roles(host: str) -> None:
    registry = default_registry(ROOT)
    if not registry.has(host, "pve-interface-pinning"):
        pytest.skip(f"{host}: pve-interface-pinning not enabled, nothing to cross-check")

    pins = pve_interface_pinning.normalize_interface_pins(registry, host)

    # Exercising the real validate() path is what actually gates ./deploy and
    # ./validate; a passing assertion here without also calling it would only prove
    # the golden data is fine today, not that drift gets caught tomorrow.
    pve_interface_pinning.validate_postinstall_alignment(registry, host, pins)


def test_postinstall_alignment_guard_actually_fires_on_drift() -> None:
    # Guard the guard: prove validate_postinstall_alignment rejects real drift instead
    # of silently passing everything.
    host = _interface_hosts()[0]
    registry = default_registry(ROOT)
    pins = pve_interface_pinning.normalize_interface_pins(registry, host)
    drifted = tuple(
        pve_interface_pinning.InterfacePin(
            name="nic9", role=pin.role, mac=pin.mac, wake_on_lan=pin.wake_on_lan
        )
        if pin.role == "management"
        else pin
        for pin in pins
    )

    with pytest.raises(ValueError, match="mgmt_iface"):
        pve_interface_pinning.validate_postinstall_alignment(registry, host, drifted)


# --------------------------------------------------------------------------------------
# pve-gpu-passthrough: boot cmdline
# --------------------------------------------------------------------------------------


def test_cmdline_always_keeps_the_root_dataset_token(tmp_path: Path) -> None:
    # Dropping root=ZFS=... from the cmdline makes the node unbootable.
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"{pve_gpu_passthrough.REQUIRED_ROOT_TOKEN} quiet\n", encoding="utf-8")

    plain = pve_gpu_passthrough.build_cmdline(cmdline, isolate_host_gpu=False)
    isolated = pve_gpu_passthrough.build_cmdline(cmdline, isolate_host_gpu=True)

    assert plain == f"{pve_gpu_passthrough.REQUIRED_ROOT_TOKEN} quiet"
    assert isolated == f"{pve_gpu_passthrough.REQUIRED_ROOT_TOKEN} quiet video=efifb:off"
    for value in (plain, isolated):
        assert pve_gpu_passthrough.REQUIRED_ROOT_TOKEN in value
        assert "\n" not in value


def test_repo_cmdline_passes_the_safety_guard() -> None:
    # The shipped configs/cmdline is what actually lands on the node.
    pve_gpu_passthrough.validate(ROOT)


def test_validate_rejects_a_cmdline_without_the_root_token(tmp_path: Path) -> None:
    configs = tmp_path / "pve-gpu-passthrough" / "configs"
    configs.mkdir(parents=True)
    for name in ("blacklist.conf", "modules", "vfio.conf.tpl"):
        (configs / name).write_text("", encoding="utf-8")
    (configs / "cmdline").write_text("quiet splash\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe cmdline"):
        pve_gpu_passthrough.validate(tmp_path)


# --------------------------------------------------------------------------------------
# pve-autoinstall: unattended installer answer files (wipes disks)
# --------------------------------------------------------------------------------------


def _autoinstall_targets() -> list[str]:
    # The PDM host carries the server-side config (pdm_host, tokens) and is not itself
    # an install target, so it has no dmi_uuid / boot_disk_serial.
    registry = default_registry(ROOT)
    return [
        host
        for host in _hosts_with("pve-autoinstall")
        if not pve_autoinstall._is_pdm_host(registry, host)
    ]


def test_there_are_autoinstall_targets_to_cover() -> None:
    assert _autoinstall_targets(), "expected at least one pve-autoinstall install target"


@pytest.mark.parametrize("host", _autoinstall_targets())
def test_autoinstall_host_config_is_complete(host: str) -> None:
    registry = default_registry(ROOT)

    # Raises with a precise message when dmi_uuid / boot_disk_serial / answer_name or
    # the network config is missing. An answer file that matches the wrong machine
    # installs over the wrong disk.
    pve_autoinstall._validate_pve_host(registry, host)

    mac = pve_autoinstall._get_mgmt_mac(registry, host)
    assert re.fullmatch(r"[0-9a-f]{12}", mac), f"{host}: mgmt mac not normalized: {mac!r}"

    serial = str(registry.get(host, "pve-autoinstall.boot_disk_serial")).strip()
    assert serial, f"{host}: empty boot_disk_serial — installer could target the wrong disk"


def test_autoinstall_identifiers_are_unique() -> None:
    # Two hosts sharing a boot disk serial or DMI UUID means an unattended install can
    # match — and wipe — the wrong machine.
    registry = default_registry(ROOT)
    for key in ("boot_disk_serial", "dmi_uuid"):
        seen: dict[str, str] = {}
        for host in _autoinstall_targets():
            value = str(registry.get(host, f"pve-autoinstall.{key}")).strip().lower()
            assert value not in seen, (
                f"{host} and {seen[value]} share {key} {value!r}: "
                "an autoinstall could target the wrong machine"
            )
            seen[value] = host
