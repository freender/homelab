"""Golden-file render tests for the modules that can take a node off the network.

`pve-postinstall` (writes /etc/network/interfaces), `pve-interface-pinning` (renames
NICs by MAC), `pve-gpu-passthrough` (rewrites the boot cmdline) and `pve-autoinstall`
(writes unattended-installer answer files) had zero test coverage. A bad render in any
of them is only discovered after a reboot, on a host you can no longer reach.

`keepalived` belongs to the same class for a different reason: it does not break the
node it runs on, it breaks the *fleet*. It owns the VIP that fronts the triple-Traefik
HA setup, and its failure modes are cross-host — a VRID mismatch or an asymmetric peer
list yields two masters answering for one address, which looks healthy from every
individual host. Nothing else in the suite compares keepalived hosts against each
other.

These render against the real hosts.conf, so they also catch inventory drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from homelab.deploy import prepare_build_dir
from homelab.hosts import default_registry
from homelab.modules import (
    keepalived,
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


def test_there_are_pinned_hosts_to_cover() -> None:
    # Guard the guard: this file guards its other three inventory-driven parametrizes
    # but this one was missed. If pve-interface-pinning were dropped from every host,
    # the test below would silently stop existing rather than fail.
    assert _hosts_with("pve-interface-pinning"), "expected at least one pinned host"


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


# --------------------------------------------------------------------------------------
# keepalived: the VIP fronting the triple-Traefik HA setup
#
# Most checks below are cross-host invariants. Each node's own config can be perfectly
# valid on its own and still produce a split-brain VIP when compared to its peers, so
# per-host validation (which keepalived.normalize_config already does) cannot catch any
# of it. Rendering offline against the real hosts.conf is what makes that comparison
# possible without SSH or 1Password.
# --------------------------------------------------------------------------------------


def _keepalived_hosts() -> list[str]:
    return _hosts_with("keepalived")


@pytest.fixture(scope="module")
def keepalived_configs() -> dict[str, keepalived.KeepalivedConfig]:
    # MonkeyPatch.context() unwinds on exit, so offline mode does not leak into the
    # rest of the module the way a bare setenv in a module-scoped fixture would.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOMELAB_OFFLINE", "1")
        registry = default_registry(ROOT)
        return {
            host: keepalived.normalize_config(ROOT, registry, host)
            for host in _keepalived_hosts()
        }


@pytest.fixture(scope="module")
def keepalived_renders(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Render every keepalived host's artifacts exactly once for the whole module."""
    renders: dict[str, Path] = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOMELAB_OFFLINE", "1")
        for host in _keepalived_hosts():
            build_dir = tmp_path_factory.mktemp(f"keepalived-{host}-")
            keepalived.build_host_artifacts(ROOT, host, build_dir)
            renders[host] = build_dir
    return renders


def test_there_are_keepalived_hosts_to_cover() -> None:
    # Guard the guard. A VIP needs at least two participants to be highly available,
    # and the fleet-wide comparisons below are vacuous with fewer.
    assert len(_keepalived_hosts()) >= 2, "expected at least two keepalived hosts"


@pytest.mark.parametrize("host", _keepalived_hosts())
def test_keepalived_render_is_complete(host: str, keepalived_renders) -> None:
    """Both artifacts must render with no placeholders left behind.

    `healthcheck.sh` is the track_script: if it renders broken it exits non-zero on
    every node, every node drops its weight, and the VIP ends up wherever priority
    alone puts it — health checking silently stops mattering.
    """
    for name in keepalived.TEMPLATE_FILES:
        rendered = (keepalived_renders[host] / name).read_text(encoding="utf-8")

        assert not UNRENDERED.search(rendered), f"{host}: placeholder left in {name}"
        assert rendered.strip(), f"{host}: {name} rendered empty"


@pytest.mark.parametrize("host", _keepalived_hosts())
def test_keepalived_conf_carries_the_hosts_own_identity(
    host: str, keepalived_configs, keepalived_renders
) -> None:
    config = keepalived_configs[host]
    rendered = (keepalived_renders[host] / "keepalived.conf").read_text(encoding="utf-8")

    assert f"vrrp_instance {config.instance_name}" in rendered
    assert f"interface {config.interface}" in rendered
    assert f"virtual_router_id {config.virtual_router_id}" in rendered
    assert f"priority {config.priority}" in rendered
    assert f"unicast_src_ip {config.unicast_src_ip}" in rendered

    # Peers and VIPs are joined with indentation into block bodies; a broken join
    # would collapse them onto one line and keepalived would refuse the config at
    # start, after the old one is already gone.
    for peer in config.unicast_peers:
        assert re.search(rf"^\s+{re.escape(peer)}\s*$", rendered, re.MULTILINE), (
            f"{host}: peer {peer} not on its own line"
        )
    for vip in config.virtual_ips:
        assert re.search(rf"^\s+{re.escape(vip)}\s*$", rendered, re.MULTILINE), (
            f"{host}: vip {vip} not on its own line"
        )


@pytest.mark.parametrize("host", _keepalived_hosts())
def test_keepalived_healthcheck_targets_the_local_backend(
    host: str, keepalived_configs, keepalived_renders
) -> None:
    config = keepalived_configs[host]
    rendered = (keepalived_renders[host] / "healthcheck.sh").read_text(encoding="utf-8")

    assert config.healthcheck_host in rendered
    assert config.healthcheck_url in rendered
    # The resolve pin is what makes this a *local* check rather than a request the
    # VIP itself could answer from another node.
    assert "--resolve" in rendered and "127.0.0.1" in rendered


def test_all_keepalived_hosts_share_one_virtual_router_id(keepalived_configs) -> None:
    """A VRID mismatch is the classic split-brain: each node forms its own VRRP group,
    every node becomes master, and the VIP is answered by all of them at once."""
    vrids = {host: c.virtual_router_id for host, c in keepalived_configs.items()}

    assert len(set(vrids.values())) == 1, f"keepalived VRID mismatch across hosts: {vrids}"


def test_keepalived_priorities_are_unique(keepalived_configs) -> None:
    """Equal priorities make mastership depend on IP tiebreak and, with preempt,
    flap the VIP between nodes."""
    seen: dict[int, str] = {}
    for host, config in keepalived_configs.items():
        assert config.priority not in seen, (
            f"{host} and {seen[config.priority]} share priority {config.priority}"
        )
        seen[config.priority] = host


def test_keepalived_peer_lists_are_symmetric_and_self_excluding(keepalived_configs) -> None:
    """Unicast VRRP is not discovered — every node must list every other node.

    A missing entry is invisible while the omitted node is BACKUP and becomes a second
    master the moment it stops hearing advertisements it was never being sent.
    """
    addresses = {host: c.unicast_src_ip for host, c in keepalived_configs.items()}

    for host, config in keepalived_configs.items():
        peers = set(config.unicast_peers)

        assert config.unicast_src_ip not in peers, f"{host}: lists itself as a unicast peer"

        expected = {ip for other, ip in addresses.items() if other != host}
        assert peers == expected, (
            f"{host}: unicast_peers {sorted(peers)} != other members {sorted(expected)}"
        )


def test_keepalived_source_addresses_are_unique(keepalived_configs) -> None:
    seen: dict[str, str] = {}
    for host, config in keepalived_configs.items():
        assert config.unicast_src_ip not in seen, (
            f"{host} and {seen[config.unicast_src_ip]} share {config.unicast_src_ip}"
        )
        seen[config.unicast_src_ip] = host


def test_keepalived_healthcheck_hosts_are_unique(keepalived_configs) -> None:
    """Each node health-checks its own Traefik via --resolve to 127.0.0.1.

    Two nodes sharing a healthcheck host would check the same backend, so a node
    would hold the VIP on the strength of a *different* node's health.
    """
    seen: dict[str, str] = {}
    for host, config in keepalived_configs.items():
        assert config.healthcheck_host not in seen, (
            f"{host} and {seen[config.healthcheck_host]} share a healthcheck host"
        )
        seen[config.healthcheck_host] = host


def test_keepalived_hosts_agree_on_the_virtual_address(keepalived_configs) -> None:
    """The VIP addresses must match across the group even though the `dev <iface>`
    suffix legitimately differs per host."""
    per_host = {
        host: sorted(vip.split()[0] for vip in c.virtual_ips)
        for host, c in keepalived_configs.items()
    }
    distinct = {tuple(v) for v in per_host.values()}

    assert len(distinct) == 1, f"keepalived hosts disagree on the VIP: {per_host}"


def test_keepalived_vip_device_matches_the_hosts_interface(keepalived_configs) -> None:
    """`virtual_ipaddress ... dev X` and `interface Y` are separate hosts.conf keys.

    VRRP would run on Y while the address is added to X, so the node advertises
    mastership for a VIP that never becomes reachable on the NIC carrying the traffic.
    """
    for host, config in keepalived_configs.items():
        for vip in config.virtual_ips:
            parts = vip.split()
            if "dev" not in parts:
                continue
            device = parts[parts.index("dev") + 1]
            assert device == config.interface, (
                f"{host}: VIP pinned to dev {device} but VRRP runs on {config.interface}"
            )


def test_keepalived_advert_intervals_agree(keepalived_configs) -> None:
    """Mismatched advert_int makes peers compute different master-down timers, which
    shows up as intermittent unexplained failovers rather than an outright break."""
    intervals = {host: c.advert_interval for host, c in keepalived_configs.items()}

    assert len(set(intervals.values())) == 1, f"advert_interval mismatch: {intervals}"


def test_keepalived_validate_rejects_a_bad_priority(offline: None) -> None:
    # Guard the guard: prove normalize_config actually rejects out-of-range values
    # instead of the golden data merely happening to be fine.
    registry = default_registry(ROOT)
    host = _keepalived_hosts()[0]

    class Drifted:
        def get(self, h: str, key: str, default: object = None) -> object:
            if key == "keepalived.priority":
                return 0
            return registry.get(h, key, default)

        def has(self, h: str, key: str) -> bool:
            return registry.has(h, key)

    with pytest.raises(ValueError, match="priority must be >= 1"):
        keepalived.normalize_config(ROOT, Drifted(), host)
