"""Render-path tests for `pve-http-boot`.

`test_pve_http_boot.py` asserts on the *source* files as text — it greps
`configs/` and `templates/` for expected strings and never invokes the module.
That is useful (it pins the iPXE contract) but it leaves the whole build path
untested: nothing checked that the templates get rendered, that the rendered
output is placeholder-free, or that what gets built matches what gets installed.

This module's failure mode is unusually unforgiving. Nothing here fails at deploy
time — `nginx -t` passes, systemd is happy, the deploy goes green. It fails when
somebody tries to netboot a bare-metal node, which is precisely the moment when
the machine has no other way in and the person is standing in a rack. So the
checks below deliberately target the "renders fine, boots nothing" class:

  * a build artifact that is never installed (or vice versa),
  * an unsubstituted `{{ MGMT_IP }}` reaching an iPXE entry point,
  * a menu chaining to a file that is not served,
  * the ISO `PLACEHOLDER` sentinel escaping the one file the autoupdate rewrites,
  * a mode regression on the file holding the PDM URL and cert fingerprint.

Everything runs against an isolated copy of the repo's module tree under
`tmp_path`, so no test writes into `pve-http-boot/build/` in the working tree.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from homelab.modules import pve_http_boot

ROOT = Path(__file__).resolve().parents[1]
MODULE = "pve-http-boot"
HOST = "arc"

UNRENDERED = re.compile(r"\{\{|\}\}|\{%")
CHAIN_TARGET = re.compile(r"chain http://\$\{http-boot-server\}/([A-Za-z0-9._-]+)")

# nginx serves /srv/httpboot as its document root, so a chain URL path of `x.ipxe`
# resolves to the file installed at /srv/httpboot/x.ipxe.
DOC_ROOT = "/srv/httpboot"


@pytest.fixture(scope="module")
def staged_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A minimal repo copy containing everything deploy_host reads.

    deploy_host writes into `<root>/pve-http-boot/build/<host>`, so pointing it at
    a copy is what keeps the real working tree clean and the test hermetic.
    """
    root = tmp_path_factory.mktemp("http-boot-root-")
    shutil.copy2(ROOT / "hosts.conf", root / "hosts.conf")
    shutil.copytree(ROOT / "secrets", root / "secrets")
    shutil.copytree(ROOT / MODULE, root / MODULE, ignore=shutil.ignore_patterns("build"))
    return root


@pytest.fixture(scope="module")
def build_dir(staged_root: Path) -> Path:
    """Run the real offline dry-run once and hand back the build output."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOMELAB_OFFLINE", "1")
        pve_http_boot.deploy_host(staged_root, HOST, dry_run=True, force=False)
    return staged_root / MODULE / "build" / HOST


@pytest.fixture(scope="module")
def mgmt_ip() -> str:
    from homelab.hosts import default_registry

    return str(default_registry(ROOT).get(HOST, "pve-http-boot.mgmt_ip"))


def built_files(build_dir: Path) -> set[str]:
    return {p.name for p in build_dir.iterdir() if p.is_file()}


# --------------------------------------------------------------------------
# build <-> install manifest
# --------------------------------------------------------------------------


def test_every_built_file_is_actually_installed(build_dir: Path) -> None:
    """A file built but absent from FILE_SPECS is silently never installed.

    This is the nastiest failure in the module: the render is correct, the deploy
    reports success, and the file simply never reaches /srv/httpboot. The node
    then netboots into whatever the previous deploy left behind.
    """
    declared = {spec.build_name for spec in pve_http_boot.FILE_SPECS}
    # file-map.conf is the manifest itself; the installer reads it rather than
    # installing it.
    orphaned = built_files(build_dir) - declared - {"file-map.conf"}

    assert not orphaned, f"built but never installed: {sorted(orphaned)}"


def test_every_installed_file_is_actually_built(build_dir: Path) -> None:
    """The loud direction of the same mismatch — install.sh aborts on a missing
    build file. Asserted anyway so the pair documents both halves."""
    declared = {spec.build_name for spec in pve_http_boot.FILE_SPECS}

    missing = declared - built_files(build_dir)

    assert not missing, f"declared in FILE_SPECS but never built: {sorted(missing)}"


def test_file_map_manifest_matches_file_specs(build_dir: Path) -> None:
    """The manifest is what the remote bash installer consumes; if it drifts from
    FILE_SPECS the host installs a different set than the module thinks it did."""
    lines = (build_dir / "file-map.conf").read_text(encoding="utf-8").splitlines()

    assert [line for line in lines if line] == [
        f"{spec.build_name}|{spec.remote_path}|{spec.mode}"
        for spec in pve_http_boot.FILE_SPECS
    ]


def test_declared_names_are_unique() -> None:
    names = [spec.build_name for spec in pve_http_boot.FILE_SPECS]
    paths = [spec.remote_path for spec in pve_http_boot.FILE_SPECS]

    assert len(names) == len(set(names)), "duplicate build_name in FILE_SPECS"
    assert len(paths) == len(set(paths)), "two build files target one remote path"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_no_unrendered_placeholder_survives_the_build(build_dir: Path) -> None:
    """A surviving `{{ MGMT_IP }}` is not a syntax error in iPXE or nginx — it is
    a literal string that produces an unreachable server at boot time."""
    for path in sorted(build_dir.iterdir()):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")

        assert not UNRENDERED.search(content), f"{path.name}: unrendered placeholder"


@pytest.mark.parametrize(
    "name",
    ["boot.ipxe", "httpboot-autoexec.ipxe", "nginx-http-boot.conf", "http-boot-mgmt.conf"],
)
def test_mgmt_ip_is_baked_into_every_host_specific_artifact(
    name: str, build_dir: Path, mgmt_ip: str
) -> None:
    content = (build_dir / name).read_text(encoding="utf-8")

    assert mgmt_ip in content, f"{name}: mgmt_ip {mgmt_ip} not rendered"


@pytest.mark.parametrize("name", pve_http_boot.IPXE_ENTRY_TEMPLATES)
def test_ipxe_entry_points_pin_the_server_before_chaining(
    name: str, build_dir: Path, mgmt_ip: str
) -> None:
    """Every downstream menu inherits ${http-boot-server}; only the entry points
    set it. If one stops setting it, the chain resolves to `http:///…`."""
    content = (build_dir / name).read_text(encoding="utf-8")

    assert f"set http-boot-server {mgmt_ip}" in content
    assert "${next-server}" not in content, f"{name}: stale DHCP next-server reference"
    assert "http:///" not in content, f"{name}: empty server in URL"


def test_menus_do_not_hardcode_the_server(build_dir: Path, mgmt_ip: str) -> None:
    """Static menus must inherit the address, not carry it.

    A hardcoded IP renders correctly for `arc` and silently breaks the moment the
    module is deployed to a second host.
    """
    for name in pve_http_boot.IPXE_MENUS:
        content = (build_dir / name).read_text(encoding="utf-8")

        assert mgmt_ip not in content, f"{name}: hardcodes the HTTP boot server IP"
        assert "set http-boot-server" not in content, f"{name}: overrides the server"


# --------------------------------------------------------------------------
# chain reachability
# --------------------------------------------------------------------------


def test_every_chained_menu_is_served(build_dir: Path) -> None:
    """Follow the actual chain targets and prove each one is deployed.

    Adding a menu entry without adding the file to FILE_SPECS produces a menu that
    looks right, deploys clean, and dead-ends at `Could not open` on the console
    of a machine that is now not booting.
    """
    served = {
        spec.remote_path
        for spec in pve_http_boot.FILE_SPECS
        if spec.remote_path.startswith(f"{DOC_ROOT}/")
    }

    referenced: dict[str, set[str]] = {}
    for path in sorted(build_dir.glob("*.ipxe")):
        for target in CHAIN_TARGET.findall(path.read_text(encoding="utf-8")):
            referenced.setdefault(target, set()).add(path.name)

    assert referenced, "no chain targets found — the regex or the menus changed"

    for target, sources in sorted(referenced.items()):
        assert f"{DOC_ROOT}/{target}" in served, (
            f"{target} is chained from {sorted(sources)} but is not installed under {DOC_ROOT}"
        )


def test_autoexec_is_served_from_the_nested_httpboot_path() -> None:
    """UEFI HTTP Boot fetches /httpboot/autoexec.ipxe, not /autoexec.ipxe.

    The build name and the remote path deliberately differ here; flattening the
    path is an easy "cleanup" that stops the firmware finding the entry point.
    """
    spec = next(
        s for s in pve_http_boot.FILE_SPECS if s.build_name == "httpboot-autoexec.ipxe"
    )

    assert spec.remote_path == f"{DOC_ROOT}/httpboot/autoexec.ipxe"


# --------------------------------------------------------------------------
# ISO placeholder sentinel
# --------------------------------------------------------------------------


def test_placeholder_iso_is_confined_to_the_shared_loader(build_dir: Path) -> None:
    """`proxmox-ve_PLACEHOLDER.iso` is the sed target for pve-http-boot-autoupdate.

    It must appear in exactly one served file. A second copy is never rewritten on
    a version bump, so that path keeps requesting an ISO that no longer exists —
    and only that one menu entry breaks, which is why it goes unnoticed.
    """
    carriers = {
        path.name
        for path in build_dir.glob("*.ipxe")
        if "PLACEHOLDER" in path.read_text(encoding="utf-8")
    }

    assert carriers == {"pve-load.ipxe"}, f"unexpected PLACEHOLDER carriers: {carriers}"


def test_loader_requests_kernel_initrd_and_iso_from_the_inherited_server(
    build_dir: Path,
) -> None:
    loader = (build_dir / "pve-load.ipxe").read_text(encoding="utf-8")

    for asset in ("vmlinuz", "initrd.img", "proxmox-ve_PLACEHOLDER.iso"):
        assert f"http://${{http-boot-server}}/{asset}" in loader, f"missing {asset}"


# --------------------------------------------------------------------------
# permissions and destinations
# --------------------------------------------------------------------------


def test_the_pdm_credential_file_is_not_world_readable() -> None:
    """http-boot-mgmt.conf carries the PDM answer URL and its TLS fingerprint.

    Everything else in this module is public boot payload served over plain HTTP;
    this one file is not, and a mode regression to the 644 default would be
    invisible.
    """
    spec = next(
        s for s in pve_http_boot.FILE_SPECS if s.build_name == "http-boot-mgmt.conf"
    )

    assert spec.mode == "600"
    assert spec.remote_path.startswith(pve_http_boot.HTTP_BOOT_CONFIG_DIR + "/")


@pytest.mark.parametrize("name", pve_http_boot.OPERATIONAL_SCRIPTS)
def test_operational_scripts_are_installed_executable(name: str) -> None:
    spec = next(s for s in pve_http_boot.FILE_SPECS if s.build_name == name)

    assert spec.mode == "755", f"{name} would install non-executable"
    assert spec.remote_path == f"/usr/local/sbin/{name}"


def test_systemd_units_land_in_the_system_unit_directory() -> None:
    units = [
        s for s in pve_http_boot.FILE_SPECS if s.build_name.endswith((".service", ".timer"))
    ]

    assert {s.build_name for s in units} == {
        "pve-http-boot-autoupdate.service",
        "pve-http-boot-autoupdate.timer",
    }
    for spec in units:
        assert spec.remote_path == f"/etc/systemd/system/{spec.build_name}"


def test_served_payload_is_not_secret_moded() -> None:
    """The boot payload is fetched by firmware over plain HTTP with no auth, so a
    restrictive mode here would just make nginx 403 instead of protecting it."""
    for spec in pve_http_boot.FILE_SPECS:
        if spec.remote_path.startswith(f"{DOC_ROOT}/"):
            assert spec.mode == "644", f"{spec.build_name}: nginx cannot read mode {spec.mode}"


# --------------------------------------------------------------------------
# autoupdate timer
# --------------------------------------------------------------------------


def test_autoupdate_timer_renders_the_inventory_schedule(build_dir: Path) -> None:
    from homelab.hosts import default_registry

    expected = str(
        default_registry(ROOT).get(HOST, "pve-http-boot.autoupdate_schedule", "*-*-* 09:00:00")
    )
    timer = (build_dir / "pve-http-boot-autoupdate.timer").read_text(encoding="utf-8")

    assert f"OnCalendar={expected}" in timer
    # Without an Install section the timer is installed but never enabled, so the
    # ISO silently stops tracking Proxmox releases.
    assert "WantedBy=timers.target" in timer
    assert "[Install]" in timer


# --------------------------------------------------------------------------
# mgmt_ip validation
# --------------------------------------------------------------------------


def test_mgmt_ip_accepts_a_bare_address() -> None:
    assert pve_http_boot.normalize_mgmt_ip("10.0.0.50", HOST) == "10.0.0.50"
    assert pve_http_boot.normalize_mgmt_ip("  10.0.0.50  ", HOST) == "10.0.0.50"


@pytest.mark.parametrize(
    "value",
    [
        "10.0.0.50/24",
        "999.999.999.999",
        "a.b.c.d",
        "10.0.0",
        "10.0.0.50.1",
        "arc.internal",
        "",
        "10.0.0.50:80",
    ],
)
def test_mgmt_ip_rejects_forms_that_would_render_an_unbootable_entry_point(
    value: str,
) -> None:
    """The CIDR case is the realistic one.

    `pve-postinstall.interfaces.mgmt_ip` in this same hosts.conf is a CIDR, so the
    two identically-named keys take different formats. The old check only counted
    dots, so `10.0.0.50/24` passed and rendered `set http-boot-server 10.0.0.50/24`
    into the iPXE entry point and `listen 10.0.0.50/24:80` into nginx.
    """
    with pytest.raises(ValueError, match="bare dotted IPv4"):
        pve_http_boot.normalize_mgmt_ip(value, HOST)


# --------------------------------------------------------------------------
# secret readers
# --------------------------------------------------------------------------


def write_secret(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rendered.env"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("reader", "key"),
    [
        (pve_http_boot._read_token, "PVE_HTTP_BOOT_TOKEN"),
        (pve_http_boot._read_pdm_cert_fingerprint, "PDM_CERT_FINGERPRINT"),
    ],
)
def test_secret_readers_return_the_value(
    reader, key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_secret(tmp_path, f"{key}=abc123\n")
    monkeypatch.setattr(pve_http_boot.op_secrets, "secret_file", lambda _r, _n: path)

    assert reader(tmp_path) == "abc123"


@pytest.mark.parametrize(
    ("reader", "key"),
    [
        (pve_http_boot._read_token, "PVE_HTTP_BOOT_TOKEN"),
        (pve_http_boot._read_pdm_cert_fingerprint, "PDM_CERT_FINGERPRINT"),
    ],
)
@pytest.mark.parametrize("body", ["{key}=\n", '{key}="   "\n', "OTHER=x\n"])
def test_secret_readers_reject_an_unresolved_value(
    reader, key: str, body: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty render must fail at deploy time, not at install time.

    A blank token still writes a token file and still deploys green; the failure
    surfaces as the PDM answer fetch being rejected mid-install, on a machine that
    has already wiped its disks.
    """
    path = write_secret(tmp_path, body.format(key=key))
    monkeypatch.setattr(pve_http_boot.op_secrets, "secret_file", lambda _r, _n: path)

    with pytest.raises(ValueError, match=f"{key} is empty"):
        reader(tmp_path)


# --------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------


def test_validate_accepts_the_real_module_tree(staged_root: Path) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOMELAB_OFFLINE", "1")
        pve_http_boot.validate(staged_root)


@pytest.mark.parametrize(
    "relative",
    [
        "configs/pve-load.ipxe",
        "configs/pve-http-boot-autoupdate",
        "configs/pve-http-boot-autoupdate.service",
        "templates/boot.ipxe",
        "templates/httpboot-autoexec.ipxe",
        "templates/nginx-http-boot.conf",
        "templates/http-boot-mgmt.conf",
        "templates/pve-http-boot-autoupdate.timer",
    ],
)
def test_validate_names_the_missing_file(
    relative: str, staged_root: Path, tmp_path: Path
) -> None:
    """Every required source is individually load-bearing, so validate must fail
    on each one rather than only on the first it happens to check."""
    root = tmp_path / "root"
    shutil.copytree(staged_root, root, ignore=shutil.ignore_patterns("build"))
    (root / MODULE / relative).unlink()

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOMELAB_OFFLINE", "1")
        with pytest.raises(ValueError, match=Path(relative).name):
            pve_http_boot.validate(root)
