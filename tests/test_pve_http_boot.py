from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.homelab.modules import pve_http_boot

ROOT = Path(__file__).resolve().parents[1]
HTTP_BOOT_CONFIGS = ROOT / "pve-http-boot" / "configs"
HTTP_BOOT_TEMPLATES = ROOT / "pve-http-boot" / "templates"

# The awk program the updater uses to pick the ISO out of SHA256SUMS. Kept
# verbatim so the behavioural test below exercises the real selection rather
# than a paraphrase of it.
SELECT_AWK = r"$2 ~ /^proxmox-ve_[0-9]+\.[0-9]+-[0-9]+\.iso$/ { print $2 }"

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


def _select(awk_program: str, manifest: str) -> str:
    """Run the updater's real `awk ... | sort -V | tail -1` selection pipeline."""
    result = subprocess.run(
        ["bash", "-c", 'awk "$1" | sort -V | tail -1', "bash", awk_program],
        input=manifest,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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


def test_entrypoint_polls_for_dhcp_instead_of_asking_once() -> None:
    """A one-shot `dhcp` boots a VM and strands real hardware.

    iPXE re-initialises the NIC after the firmware hands off, so the switch port
    renegotiates and stays blocked until STP converges. A single `dhcp` returns
    failure well before that, `chain` never runs, and the node drops to the shell
    without ever requesting boot.ipxe — which is exactly how ace failed while the
    virtio test VM, whose link comes up instantly, sailed through.
    """
    autoexec = read_template("httpboot-autoexec.ipxe")

    # Every interface up first: the first enumerated NIC is not necessarily the
    # cabled one on a multi-NIC node.
    assert "\nifopen\n" in autoexec

    # A retry loop, not a bare `dhcp` followed straight by the chain.
    assert "dhcp && goto dhcp_ok" in autoexec
    assert "goto dhcp_retry" in autoexec
    assert "inc attempts -1" in autoexec
    assert "\ndhcp\nifopen\n" not in autoexec


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
    assert '"$latest" "$iso_sha256" "$PDM_URL" "$PDM_CERT_FINGERPRINT"' in updater
    assert 'sha256sum <"$TOKEN_FILE"' in updater
    assert '"$want_stamp" == "$have_stamp"' in updater


def test_build_stamp_includes_the_iso_digest_not_just_its_name() -> None:
    """Upstream republishing the same filename with different content would
    otherwise read as "up to date" forever, because the freshness decision is
    made before the ISO is ever fetched."""
    updater = read_config("pve-http-boot-autoupdate")

    assert '"$iso_sha256"' in updater
    stamp = updater[updater.index("want_stamp=") : updater.index("have_stamp=")]
    assert "$iso_sha256" in stamp
    assert "$latest" in stamp


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


def test_discovery_reads_the_signed_directory_manifest_not_the_index_page() -> None:
    """The /iso/ index is a hand-styled HTML document, so scraping it made
    discovery depend on presentation markup. SHA256SUMS is one line-oriented
    artifact that carries both the filename and the digest, which also removes
    the second fetch whose failure used to downgrade the run."""
    updater = read_config("pve-http-boot-autoupdate")

    assert '"${ISO_INDEX}SHA256SUMS"' in updater

    code = [line for line in updater.splitlines() if not line.lstrip().startswith("#")]
    # No HTML scrape, and no per-ISO checksum side fetch.
    assert not [line for line in code if 'curl -fsSL "$ISO_INDEX"' in line]
    assert not [line for line in code if "sum_file=" in line]
    assert not [line for line in code if "have_sums" in line]


def test_an_unverifiable_iso_aborts_instead_of_being_built() -> None:
    """The old code treated a failed checksum fetch as "no checksum available"
    and carried on, so a transient network fault was enough to turn an
    unverified ISO into the payload unattended machines install from."""
    updater = read_config("pve-http-boot-autoupdate")

    assert 'fail "could not fetch ${ISO_INDEX}SHA256SUMS"' in updater
    assert 'fail "no single sha256 digest for $latest in SHA256SUMS"' in updater
    assert 'fail "download failed for $latest"' in updater


def test_a_failed_checksum_discards_the_iso_so_the_next_run_starts_clean() -> None:
    """The download resumes with --continue-at, so leaving a corrupt file behind
    would make every later run resume onto the same bad bytes."""
    updater = read_config("pve-http-boot-autoupdate")

    mismatch = updater.index('fail "sha256 mismatch for $latest')
    discard = updater.index('rm -f "$ISO_DIR/$latest"')

    assert discard < mismatch


def test_large_download_is_bounded_retried_and_resumable() -> None:
    """A bare curl with no timeout is what lets a run wedge forever, and with no
    resume a blip at 90% throws away the whole 1.7 GB."""
    updater = read_config("pve-http-boot-autoupdate")

    for opt in ("--connect-timeout", "--max-time", "--retry", "--continue-at"):
        assert opt in updater, f"{opt} missing from the ISO fetch"

    # Every curl in the script goes through a bounded option array. An unbounded
    # one anywhere reintroduces the wedge, including the loopback probes: nginx
    # hanging would stall the run while it still holds the lock.
    bounded = ("CURL_SMALL", "CURL_ISO", "CURL_PROBE")
    code = [line for line in updater.splitlines() if not line.lstrip().startswith("#")]
    unbounded = [
        line
        for line in code
        if "curl " in line and not any(arr in line for arr in bounded)
    ]
    assert not unbounded, f"unbounded curl call(s): {unbounded}"


def test_a_checksum_mismatch_aborts_instead_of_warning() -> None:
    """The payload is what unattended machines wipe themselves and install from."""
    updater = read_config("pve-http-boot-autoupdate")

    assert 'fail "sha256 mismatch for $latest' in updater


def test_manifest_selection_picks_amd64_and_never_the_arm64_image() -> None:
    """The manifest lists every Proxmox product plus an arm64 PVE image, and
    `sort -V` orders `proxmox-ve_9.2-1-arm64.iso` *after* `proxmox-ve_9.2-1.iso`.
    A loose pattern therefore selects the ARM image for this x86 fleet, builds a
    payload from it without complaint, and only fails once a node has netbooted
    it. Run the real selection against a realistic manifest rather than trusting
    the regex by eye.
    """
    updater = read_config("pve-http-boot-autoupdate")
    assert SELECT_AWK in updater, "selection program changed; update this test"

    manifest = "\n".join(
        f"{'0' * 64}  {name}"
        for name in (
            "proxmox-ve_7.4-1.iso",
            "proxmox-ve_8.4-1.iso",
            "proxmox-ve_9.1-1.iso",
            "proxmox-ve_9.2-1-arm64.iso",
            "proxmox-ve_9.2-1.iso",
            "proxmox-backup-server_4.2-1.iso",
            "proxmox-mail-gateway_9.1-1.iso",
            "proxmox-datacenter-manager_1.1-1.iso",
        )
    )

    assert _select(SELECT_AWK, manifest) == "proxmox-ve_9.2-1.iso"

    # The trap this anchoring exists to prevent, demonstrated rather than asserted
    # in a comment: drop the anchors and the ARM image wins.
    loose = "$2 ~ /proxmox-ve_/ { print $2 }"
    assert _select(loose, manifest) == "proxmox-ve_9.2-1-arm64.iso"


# --------------------------------------------------------------------------
# lock handling: contention must not mask a stuck run
# --------------------------------------------------------------------------


def test_lock_file_is_opened_without_truncating_the_holders_timestamp() -> None:
    """`9>` truncates before flock is even attempted, which would erase the
    running instance's start time and make the stall check below blind."""
    updater = read_config("pve-http-boot-autoupdate")

    assert 'exec 9>>"$LOCK"' in updater
    assert 'exec 9>"$LOCK"' not in updater
    assert "printf 'pid=%s started=%s\\n'" in updater


def test_a_stuck_run_is_reported_instead_of_being_reported_as_success() -> None:
    """This is the one failure mode no alert could see: a wedged run holds the
    lock, every later run exits 0 "another run is active", and the payload
    silently stops tracking upstream with no failed unit anywhere."""
    updater = read_config("pve-http-boot-autoupdate")

    assert "STALL_AFTER=" in updater
    assert "age > STALL_AFTER" in updater
    assert 'fail "a run started $((age / 60))m ago still holds $LOCK' in updater


def test_cleanup_trap_is_armed_only_after_the_lock_is_held() -> None:
    """Armed earlier, a run exiting on contention wiped $STAGE and
    $HTTP_BOOT_BUILD out from under the instance holding the lock."""
    updater = read_config("pve-http-boot-autoupdate")

    lock_taken = updater.index("if ! flock -n 9; then")
    trap_armed = updater.index("trap cleanup_temp EXIT")

    assert lock_taken < trap_armed


def test_oneshot_unit_has_an_explicit_start_timeout() -> None:
    """systemd disables TimeoutStartSec for Type=oneshot by default, so without
    this a hung run is never killed and holds the lock indefinitely."""
    unit = read_config("pve-http-boot-autoupdate.service")

    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=" in unit


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


def test_installer_serves_the_snp_bound_loader_not_the_native_driver_build() -> None:
    """ipxe.efi drives the NIC itself and has never booted this fleet's hardware.

    Taking over the card means resetting it, so the link drops and has to
    renegotiate. That is free on virtio and fatal on the HP 560SFP+ (Intel
    82599) every node here boots from — iPXE re-inits the card and then stalls
    on "Waiting for link-up". arc's access log shows two VM boots completing the
    full chain and zero bare-metal ones. snponly.efi binds to the UEFI Simple
    Network Protocol instead, reusing the option-ROM driver that already has the
    link up and just fetched this file.
    """
    installer = read_installer()

    assert "install -m 0644 /usr/lib/ipxe/snponly.efi" in installer
    assert "install -m 0644 /usr/lib/ipxe/ipxe.efi" not in installer
    assert "[[ -f /usr/lib/ipxe/snponly.efi ]] || missing_pkgs+=(ipxe)" in installer


def test_loader_is_still_published_at_the_url_unifi_hands_out() -> None:
    """The DHCP boot option points at /httpboot/ipxe.efi.

    Swapping the source binary must not rename the served file, or every client
    404s until someone edits the UniFi Network Boot option by hand.
    """
    installer = read_installer()

    assert "/srv/httpboot/httpboot/ipxe.efi" in installer


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


# --------------------------------------------------------------------------
# PDM answer-auth token format
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["barestring", ":onlysecret", "onlyname:", ":", ""],
)
def test_malformed_pdm_token_is_rejected_at_deploy_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A token missing its `<name>:` half authenticates against nothing.

    The installer sends it verbatim as `Authorization: Bearer <name>:<secret>`
    and PDM resolves the name against tokens.cfg to choose a hash. Without a
    name there is nothing to resolve, so PDM returns a bare 401. prepare-iso
    bakes in whatever string it is handed, so the mistake stays invisible until
    a node has already netbooted and is asking for its answer file.
    """
    monkeypatch.setattr(
        pve_http_boot.op_secrets, "secret_file", lambda _root, _name: tmp_path / "s.env"
    )
    monkeypatch.setattr(
        pve_http_boot.op_secrets,
        "parse_env_file",
        lambda _p: {"PVE_HTTP_BOOT_TOKEN": value},
    )

    with pytest.raises(ValueError):
        pve_http_boot._read_token(ROOT)


def test_wellformed_pdm_token_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pve_http_boot.op_secrets, "secret_file", lambda _root, _name: tmp_path / "s.env"
    )
    monkeypatch.setattr(
        pve_http_boot.op_secrets,
        "parse_env_file",
        lambda _p: {"PVE_HTTP_BOOT_TOKEN": "homelab-pve-auto-install:s3cr3t"},
    )

    assert pve_http_boot._read_token(ROOT) == "homelab-pve-auto-install:s3cr3t"
