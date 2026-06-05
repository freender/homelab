from __future__ import annotations

from pathlib import Path

from homelab.templates import render_template

ROOT = Path(__file__).resolve().parents[1]
PXE_CONFIGS = ROOT / "pve-pxe" / "configs"


def read_config(name: str) -> str:
    return (PXE_CONFIGS / name).read_text(encoding="utf-8")


def test_dnsmasq_proxy_range_uses_dnsmasq_supported_syntax(tmp_path: Path) -> None:
    output = tmp_path / "dnsmasq-pxe.conf"

    render_template(
        ROOT / "pve-pxe" / "templates" / "dnsmasq-pxe.conf",
        output,
        MGMT_IP="10.0.0.51",
        MGMT_NETWORK="10.0.0.0/24",
        MGMT_PROXY_NETWORK="10.0.0.0",
    )

    rendered = output.read_text(encoding="utf-8")
    assert "dhcp-range=10.0.0.0,proxy,255.255.255.0" in rendered
    assert "dhcp-range=10.0.0.0/24,proxy" not in rendered


def test_tftp_entrypoint_uses_explicit_http_server() -> None:
    autoexec = read_config("autoexec.ipxe")

    assert "chain http://10.0.0.51/boot.ipxe" in autoexec
    assert "${next-server}" not in autoexec
    assert "http:///" not in autoexec


def test_ipxe_scripts_do_not_ship_stale_server_or_iso_placeholders() -> None:
    for path in PXE_CONFIGS.glob("*.ipxe"):
        script = path.read_text(encoding="utf-8")
        assert "${next-server}" not in script, path.name
        assert "http:///" not in script, path.name
        assert "PLACEHOLDER" not in script, path.name


def test_pdm_auto_path_sets_proxmox_auto_installer_flag() -> None:
    pdm_auto = read_config("pdm-auto.ipxe")

    assert "ip=dhcp" in pdm_auto
    assert "proxmox-start-auto-installer" in pdm_auto
    assert "chain http://10.0.0.51/pve-load.ipxe" in pdm_auto


def test_shared_loader_loads_kernel_initrd_and_iso_together() -> None:
    loader = read_config("pve-load.ipxe")

    assert "kernel http://10.0.0.51/vmlinuz initrd=initrd.img ${pve-kargs}" in loader
    assert "initrd --name initrd.img http://10.0.0.51/initrd.img" in loader
    assert "initrd http://10.0.0.51/proxmox-ve_" in loader
    assert "proxmox.iso" in loader
    assert "boot" in loader


def test_interactive_installer_entries_chain_through_shared_loader() -> None:
    expected_flags = {
        "pve-tui.ipxe": "proxtui",
        "pve-debug.ipxe": "proxdebug",
        "pve-serial.ipxe": "console=ttyS0,115200",
    }

    for name in ("pve-tui.ipxe", "pve-gui.ipxe", "pve-debug.ipxe", "pve-serial.ipxe"):
        script = read_config(name)
        assert "chain http://10.0.0.51/pve-load.ipxe" in script
        assert "kernel http://" not in script
        assert "initrd http://" not in script

    for name, flag in expected_flags.items():
        assert flag in read_config(name)


def test_pxe_autoupdate_rewrites_and_validates_promoted_ipxe_payloads() -> None:
    updater = read_config("pxe-autoupdate")

    assert 'PXE_HTTP_BASE="http://${PXE_MGMT_IP}"' in updater
    assert r's#http://\${next-server}#${PXE_HTTP_BASE}#g' in updater
    assert 's#http:///##g' in updater
    assert r's#proxmox-ve_PLACEHOLDER\.iso#${latest}#g' in updater
    assert r'grep -R "PLACEHOLDER\|\${next-server}" "$STAGE"/*.ipxe' in updater
    assert "staged iPXE scripts still contain placeholder server or ISO references" in updater
