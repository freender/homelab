from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTTP_BOOT_CONFIGS = ROOT / "pve-http-boot" / "configs"
HTTP_BOOT_TEMPLATES = ROOT / "pve-http-boot" / "templates"


def read_config(name: str) -> str:
    return (HTTP_BOOT_CONFIGS / name).read_text(encoding="utf-8")


def read_template(name: str) -> str:
    return (HTTP_BOOT_TEMPLATES / name).read_text(encoding="utf-8")


def test_httpboot_entrypoint_template_sets_http_boot_server_from_mgmt_ip() -> None:
    autoexec = read_template("httpboot-autoexec.ipxe")

    # The entry point bakes in the per-host server IP once, then every
    # downstream chain inherits it via iPXE's persistent ${http-boot-server}.
    assert "set http-boot-server {{ MGMT_IP }}" in autoexec
    assert "chain http://${http-boot-server}/boot.ipxe" in autoexec
    assert "${next-server}" not in autoexec
    assert "http:///" not in autoexec


def test_boot_menu_template_sets_http_boot_server_and_chains_through_it() -> None:
    boot = read_template("boot.ipxe")

    assert "set http-boot-server {{ MGMT_IP }}" in boot
    assert "chain http://${http-boot-server}/pdm-auto-warning.ipxe" in boot
    for name in ("pve-tui", "pve-gui", "pve-debug", "pve-serial"):
        assert f"chain http://${{http-boot-server}}/{name}.ipxe" in boot


def test_ipxe_scripts_resolve_server_at_runtime_and_avoid_stale_placeholders() -> None:
    for path in HTTP_BOOT_CONFIGS.glob("*.ipxe"):
        script = path.read_text(encoding="utf-8")
        assert "${next-server}" not in script, path.name
        assert "http:///" not in script, path.name
        # Menus must not hardcode the server; they inherit ${http-boot-server}.
        assert "10.0.0.50" not in script, path.name
        if path.name == "pve-load.ipxe":
            assert "proxmox-ve_PLACEHOLDER.iso" in script
        else:
            assert "PLACEHOLDER" not in script, path.name


def test_pdm_auto_path_sets_proxmox_auto_installer_flag() -> None:
    pdm_auto = read_config("pdm-auto.ipxe")

    assert "ip=dhcp" in pdm_auto
    assert "proxmox-start-auto-installer" in pdm_auto
    assert "chain http://${http-boot-server}/pve-load.ipxe" in pdm_auto


def test_shared_loader_loads_kernel_initrd_and_iso_together() -> None:
    loader = read_config("pve-load.ipxe")

    assert "kernel http://${http-boot-server}/vmlinuz initrd=initrd.img ${pve-kargs}" in loader
    assert "initrd --name initrd.img http://${http-boot-server}/initrd.img" in loader
    assert "initrd http://${http-boot-server}/proxmox-ve_PLACEHOLDER.iso" in loader
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
        assert "chain http://${http-boot-server}/pve-load.ipxe" in script
        assert "kernel http://" not in script
        assert "initrd http://" not in script

    for name, flag in expected_flags.items():
        assert flag in read_config(name)


def test_http_boot_autoupdate_rewrites_only_iso_filename_and_validates_payload() -> None:
    updater = read_config("pve-http-boot-autoupdate")

    # The server is resolved at runtime via ${http-boot-server}, so the only
    # per-version rewrite is the promoted ISO filename. The legacy
    # next-server / full-URL rewrites must be gone.
    assert "${next-server}" not in updater
    assert "http:///" not in updater
    assert "PXE_HTTP_BASE" not in updater
    assert r's#proxmox-ve_[^[:space:]]+\.iso#${prepared_iso}#g' in updater
    assert 'install -m 0644 "$HTTP_BOOT_BUILD/$prepared_iso" "$STAGE/$prepared_iso"' in updater
    assert r'grep -R "PLACEHOLDER" "$STAGE"/*.ipxe' in updater
    assert "staged iPXE scripts still contain placeholder ISO references" in updater


def test_http_boot_autoupdate_does_not_duplicate_baked_iso_dir() -> None:
    updater = read_config("pve-http-boot-autoupdate")

    assert 'trap cleanup_temp EXIT' in updater
    assert 'rm -rf "$STAGE" "$HTTP_BOOT_BUILD"' in updater
    assert "rsync -a --exclude '/iso/' \"$SRV\"/ \"$STAGE\"/" in updater
    assert "rsync -a --exclude '/iso/' \"$SRV\"/ \"$SRV.prev\"/" in updater
    assert "rsync -a --delete --exclude '/iso/' \"$STAGE\"/ \"$SRV\"/" in updater
    assert "rsync -a --delete --exclude '/iso/' \"$SRV.prev\"/ \"$SRV\"/" in updater
