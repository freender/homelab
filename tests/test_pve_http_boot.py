from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTTP_BOOT_CONFIGS = ROOT / "pve-http-boot" / "configs"
HTTP_BOOT_TEMPLATES = ROOT / "pve-http-boot" / "templates"

# Kernel command-line flags the Proxmox installer renamed in 8.2. An unknown flag
# is ignored rather than rejected, so a stale one does not fail the boot — it just
# silently drops you into the graphical installer, which on a headless serial-only
# node means an unusable install. The hand-rolled menus carried these for months.
LEGACY_INSTALLER_KARGS = ("proxtui", "proxdebug")


def read_config(name: str) -> str:
    return (HTTP_BOOT_CONFIGS / name).read_text(encoding="utf-8")


def read_template(name: str) -> str:
    return (HTTP_BOOT_TEMPLATES / name).read_text(encoding="utf-8")


def read_installer() -> str:
    return (ROOT / "pve-http-boot" / "scripts" / "install.sh").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the deploy owns exactly one iPXE file
# --------------------------------------------------------------------------


def test_the_boot_menu_is_not_authored_in_this_repo() -> None:
    """The menu is whatever `prepare-iso --pxe-loader ipxe` emits for the served ISO.

    Re-adding a menu here would reintroduce the drift this migration removed: a
    hand-written entry pins kernel args at the moment it was written, while the
    ISO it boots keeps moving.
    """
    assert not list(HTTP_BOOT_CONFIGS.glob("*.ipxe"))
    assert [p.name for p in HTTP_BOOT_TEMPLATES.glob("*.ipxe")] == [
        "httpboot-autoexec.ipxe"
    ]


def test_no_legacy_installer_kargs_survive_anywhere_in_the_module() -> None:
    module = ROOT / "pve-http-boot"
    for path in sorted(module.rglob("*")):
        if not path.is_file() or "build" in path.parts:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for karg in LEGACY_INSTALLER_KARGS:
            # Substring match would hit the comment that explains the rename.
            assert f" {karg}" not in content, f"{path.name}: legacy karg {karg}"


def test_httpboot_entrypoint_template_sets_http_boot_server_from_mgmt_ip() -> None:
    autoexec = read_template("httpboot-autoexec.ipxe")

    # The entry point bakes in the per-host server IP once; the stock menu it
    # chains to uses relative paths, which iPXE resolves against this same URI.
    assert "set http-boot-server {{ MGMT_IP }}" in autoexec
    assert "chain http://${http-boot-server}/boot.ipxe" in autoexec
    assert "${next-server}" not in autoexec
    assert "http:///" not in autoexec


def test_entrypoint_falls_back_to_a_shell_when_no_payload_is_built_yet() -> None:
    """boot.ipxe only exists after a successful autoupdate run.

    Without the fallback, a netboot before the first build leaves the firmware
    sitting on a failed chain with no prompt — on a machine that by definition has
    nothing else to boot from.
    """
    assert "chain http://${http-boot-server}/boot.ipxe || shell" in read_template(
        "httpboot-autoexec.ipxe"
    )


# --------------------------------------------------------------------------
# autoupdate: install stock output, never rewrite it
# --------------------------------------------------------------------------


def test_autoupdate_installs_the_stock_menu_verbatim() -> None:
    updater = read_config("pve-http-boot-autoupdate")

    assert "--pxe --pxe-loader ipxe --fetch-from http" in updater
    assert '"$HTTP_BOOT_BUILD/boot.ipxe"' in updater
    assert '"$STAGE/boot.ipxe"' in updater
    assert 'grep -q "${prepared_iso}" "$STAGE/boot.ipxe"' in updater


def test_autoupdate_never_rewrites_ipxe_content() -> None:
    """The whole point of the migration: no per-version sed step.

    The old script rewrote a PLACEHOLDER ISO name into pve-load.ipxe on every
    version bump, and install.sh carried a duplicate of that logic for fresh
    hosts. Both are gone; prepare-iso bakes the filename in.

    PLACEHOLDER itself still appears in the updater, but only inside the guard
    that rejects it — asserted separately below.
    """
    assert "sed -i" not in read_config("pve-http-boot-autoupdate")
    assert "sed -i" not in read_installer()


def test_autoupdate_rejects_a_reintroduced_hand_written_iso_reference() -> None:
    updater = read_config("pve-http-boot-autoupdate")

    assert "grep -rlE 'proxmox-ve_|PLACEHOLDER' \"$STAGE\" --include='*.ipxe'" in updater
    assert "unexpected hand-written ISO reference" in updater


# --------------------------------------------------------------------------
# build stamp
# --------------------------------------------------------------------------


def test_build_stamp_covers_every_input_baked_into_the_prepared_iso() -> None:
    """Keying freshness on the ISO version alone hides credential rotation.

    prepare-iso bakes the PDM URL, the TLS cert fingerprint and the auth token
    into the ISO. If only the version is compared, rotating the cert or the token
    leaves the stale payload reporting "up to date" until the next Proxmox
    release, and the automated install then fails to fetch its answer mid-rebuild.
    """
    updater = read_config("pve-http-boot-autoupdate")

    assert 'STAMP_FILE="/etc/homelab-http-boot/build-stamp"' in updater
    assert '"$latest" "$PDM_URL" "$PDM_CERT_FINGERPRINT"' in updater
    assert 'sha256sum <"$TOKEN_FILE"' in updater
    assert '"$want_stamp" == "$have_stamp"' in updater


def test_build_stamp_is_not_served_over_http() -> None:
    """/srv/httpboot is exported unauthenticated to the whole management VLAN."""
    updater = read_config("pve-http-boot-autoupdate")
    stamp_line = next(
        line for line in updater.splitlines() if line.startswith("STAMP_FILE=")
    )

    assert "/srv/httpboot" not in stamp_line


def test_build_stamp_is_written_only_after_the_payload_is_proven_serving() -> None:
    """A stamp written before the smoke test would mark a rolled-back payload as
    current, and the next run would skip the rebuild that fixes it."""
    updater = read_config("pve-http-boot-autoupdate")

    rollback = updater.index('fail "rolled back to previous payload"')
    stamp_write = updater.index('> "$STAMP_FILE"')

    assert rollback < stamp_write
    assert 'chmod 600 "$STAMP_FILE"' in updater


def test_incomplete_payload_forces_a_rebuild_even_when_the_stamp_matches() -> None:
    """This is what makes the migration self-healing.

    arc already reports "up to date" for the current ISO, so a version-only check
    would never build the stock menu the new entry point chains to.
    """
    updater = read_config("pve-http-boot-autoupdate")

    assert "payload_complete=1" in updater
    assert 'for f in vmlinuz initrd.img boot.ipxe; do' in updater
    assert '"$payload_complete" -eq 1' in updater


def test_autoupdate_reuses_a_verified_local_iso_instead_of_refetching() -> None:
    """A cert/token rotation rebuilds from the same upstream ISO; re-downloading
    1.7 GB to produce a byte-identical input is pure waste."""
    updater = read_config("pve-http-boot-autoupdate")

    assert "iso_verified()" in updater
    assert "if iso_verified; then" in updater
    assert "reusing verified local copy" in updater


def test_iso_checksum_uses_the_extension_upstream_actually_publishes() -> None:
    """Proxmox publishes '<iso>.sha256'. Asking for '.sha256sum' 404s, and the
    old code treated that as "no checksum available" and carried on — so the ISO
    behind every network install was never actually verified."""
    updater = read_config("pve-http-boot-autoupdate")

    assert 'sum_file="${latest}.sha256"' in updater

    code = [
        line for line in updater.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not [line for line in code if ".sha256sum" in line]


def test_a_checksum_mismatch_aborts_instead_of_warning() -> None:
    """The payload is what unattended machines wipe themselves and install from."""
    updater = read_config("pve-http-boot-autoupdate")

    assert 'fail "sha256 mismatch for $latest' in updater


def test_http_boot_autoupdate_cleans_temp_and_promotes_whole_tree() -> None:
    updater = read_config("pve-http-boot-autoupdate")

    assert 'trap cleanup_temp EXIT' in updater
    assert 'rm -rf "$STAGE" "$HTTP_BOOT_BUILD"' in updater
    assert 'rsync -a "$SRV"/ "$STAGE"/' in updater
    assert 'rsync -a "$SRV"/ "$SRV.prev"/' in updater
    assert 'rsync -a --delete "$STAGE"/ "$SRV"/' in updater
    assert 'rsync -a --delete "$SRV.prev"/ "$SRV"/' in updater


# --------------------------------------------------------------------------
# installer migration behaviour
# --------------------------------------------------------------------------


def test_installer_removes_the_superseded_menus() -> None:
    installer = read_installer()

    for name in (
        "pve-load.ipxe",
        "pdm-auto.ipxe",
        "pdm-auto-warning.ipxe",
        "pve-tui.ipxe",
        "pve-gui.ipxe",
        "pve-debug.ipxe",
        "pve-serial.ipxe",
    ):
        assert f"/srv/httpboot/{name}" in installer, f"{name} left served"


def test_installer_removes_the_old_boot_menu_only_by_content_match() -> None:
    """After migration this path holds the stock menu, which the deploy does not
    ship. Removing it unconditionally would break netboot on every re-deploy
    until the next autoupdate run."""
    installer = read_installer()

    assert 'grep -q "Homelab Network Boot" /srv/httpboot/boot.ipxe' in installer


def test_installer_builds_a_payload_when_none_is_present() -> None:
    """The timer is weekly, so waiting for it would leave netboot dropping to a
    shell for up to seven days after a fresh deploy."""
    installer = read_installer()

    assert "if [[ ! -s /srv/httpboot/boot.ipxe ]]; then" in installer
    assert "systemctl start --no-block pve-http-boot-autoupdate.service" in installer


def test_baked_offsite_iso_build_is_retired() -> None:
    """The offsite hosts run Ubuntu now; nothing may rebuild or serve baked ISOs."""
    assert not (HTTP_BOOT_CONFIGS / "iso-autobuild").exists()
    assert not (HTTP_BOOT_CONFIGS / "iso-autobuild.service").exists()
    assert "iso-autobuild" not in read_config("pve-http-boot-autoupdate")
    assert "location /iso/" not in read_template("nginx-http-boot.conf")

    installer = read_installer()
    assert "rm -rf /srv/httpboot/iso" in installer
    assert "rm -rf /etc/homelab-http-boot/iso-answers" in installer
