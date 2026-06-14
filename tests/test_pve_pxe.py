from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PXE_CONFIGS = ROOT / "pve-pxe" / "configs"


def read_config(name: str) -> str:
    return (PXE_CONFIGS / name).read_text(encoding="utf-8")


def test_httpboot_entrypoint_uses_explicit_http_server() -> None:
    autoexec = read_config("httpboot-autoexec.ipxe")

    assert "chain http://10.0.0.50/boot.ipxe" in autoexec
    assert "${next-server}" not in autoexec
    assert "http:///" not in autoexec


def test_ipxe_scripts_do_not_ship_stale_server_or_iso_placeholders() -> None:
    for path in PXE_CONFIGS.glob("*.ipxe"):
        script = path.read_text(encoding="utf-8")
        assert "${next-server}" not in script, path.name
        assert "http:///" not in script, path.name
        if path.name == "pve-load.ipxe":
            assert "proxmox-ve_PLACEHOLDER.iso" in script
        else:
            assert "PLACEHOLDER" not in script, path.name


def test_pdm_auto_path_sets_proxmox_auto_installer_flag() -> None:
    pdm_auto = read_config("pdm-auto.ipxe")

    assert "ip=dhcp" in pdm_auto
    assert "proxmox-start-auto-installer" in pdm_auto
    assert "chain http://10.0.0.50/pve-load.ipxe" in pdm_auto


def test_shared_loader_loads_kernel_initrd_and_iso_together() -> None:
    loader = read_config("pve-load.ipxe")

    assert "kernel http://10.0.0.50/vmlinuz initrd=initrd.img ${pve-kargs}" in loader
    assert "initrd --name initrd.img http://10.0.0.50/initrd.img" in loader
    assert "initrd http://10.0.0.50/proxmox-ve_PLACEHOLDER.iso" in loader
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
        assert "chain http://10.0.0.50/pve-load.ipxe" in script
        assert "kernel http://" not in script
        assert "initrd http://" not in script

    for name, flag in expected_flags.items():
        assert flag in read_config(name)


def test_pxe_autoupdate_rewrites_and_validates_promoted_ipxe_payloads() -> None:
    updater = read_config("pxe-autoupdate")

    assert 'PXE_HTTP_BASE="http://${PXE_MGMT_IP}"' in updater
    assert r's#http://\${next-server}#${PXE_HTTP_BASE}#g' in updater
    assert 's#http:///##g' in updater
    assert r's#proxmox-ve_PLACEHOLDER\.iso#${prepared_iso}#g' in updater
    assert 'install -m 0644 "$PXE_BUILD/$prepared_iso" "$STAGE/$prepared_iso"' in updater
    assert r'grep -R "PLACEHOLDER\|\${next-server}" "$STAGE"/*.ipxe' in updater
    assert "staged iPXE scripts still contain placeholder server or ISO references" in updater


def test_pxe_autoupdate_does_not_duplicate_baked_iso_dir() -> None:
    updater = read_config("pxe-autoupdate")

    assert 'trap cleanup_temp EXIT' in updater
    assert 'rm -rf "$STAGE" "$PXE_BUILD"' in updater
    assert "rsync -a --exclude '/iso/' \"$SRV\"/ \"$STAGE\"/" in updater
    assert "rsync -a --exclude '/iso/' \"$SRV\"/ \"$SRV.prev\"/" in updater
    assert "rsync -a --delete --exclude '/iso/' \"$STAGE\"/ \"$SRV\"/" in updater
    assert "rsync -a --delete --exclude '/iso/' \"$SRV.prev\"/ \"$SRV\"/" in updater
