"""vr-guest: solar's guest-side setup (freender/homelab-ops#400).

Three parts, three loaders: the orchestrator is a package module; the remote
installer and `solar-check` are scripts loaded by path. The check ids are pinned
literally because they are the `check` label of homelab_solar_check: renaming one
silently splits its history and the SolarPatchCheckFailed series in solar.yml.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from homelab.modules import vr_guest
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "vr-guest" / "scripts" / "install.py"
CHECK_PATH = ROOT / "vr-guest" / "files" / "libexec" / "solar-check"
METRICS_PATH = ROOT / "vr-guest" / "files" / "libexec" / "solar-metrics"

# Every id the old setup.sh check printed except the vr-stage ones, which come from
# solar-check itself rather than the manifest.
MANIFEST_IDS = [
    "/etc/ssh/sshd_config.d/10-key-only.conf",
    "/etc/plasmalogin.conf.d/autologin.conf",
    "/etc/udev/rules.d/70-xrhardware.rules",
    "/etc/udev/rules.d/71-psvr2-steamvr.rules",
    "/etc/systemd/system/node_exporter.service",
    "/etc/systemd/system/solar-metrics.service",
    "/etc/systemd/system/solar-metrics.timer",
    "home/.local/bin/psvr2-steamvr",
    "home/.local/bin/psvr2-audio",
    "home/.config/systemd/user/psvr2-steamvr.service",
    "solar_metrics_script",
]
CHECK_IDS = [
    "hostname", "karg:nvidia-modeset.conceal_vrr_caps=1", "karg:bluetooth.disable_ertm=1",
    "conceal_vrr_caps", *MANIFEST_IDS, "steam_library", "sshd", "steamvr_linux_fixes",
    "ignition_shim", "psvr2toolkit_dll", "openvr_driver", "compositor_cap_sys_nice",
    "vrmonitor_handler", "openxr_runtime", "steamvr_settings", "patched_bluetoothd",
    "bluez_build_matches", "node_exporter_version", "solar_metrics_timer",
]


def load_script(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

SOLAR = """solar:
  config:
    type: {type}
    hostname: solar.internal
    user: root
    sshkey: infra
{ssh_config}  features:
    vr-guest:
"""
DESKTOP = "    ssh_config:\n      user: freender\n"


def fake_root(tmp_path: Path, os_type: str = "bazzite", ssh_config: str = DESKTOP) -> Path:
    (tmp_path / "hosts.conf").write_text(
        SOLAR.format(type=os_type, ssh_config=ssh_config), encoding="utf-8"
    )
    shutil.copytree(ROOT / "vr-guest", tmp_path / "vr-guest",
                    ignore=shutil.ignore_patterns("build", "__pycache__"))
    return tmp_path


def test_validate_accepts_the_real_inventory() -> None:
    from homelab.hosts import default_registry

    hosts = default_registry(ROOT).list_hosts(vr_guest.MODULE_DIR)
    assert hosts == ["solar"]
    vr_guest.validate(ROOT, hosts)


def test_validate_requires_every_module_file(tmp_path: Path) -> None:
    root = fake_root(tmp_path)
    (root / "vr-guest" / "files" / "libexec" / "solar-check").unlink()

    with pytest.raises(ValueError, match="missing module file: .*solar-check"):
        vr_guest.validate(root, ["solar"])


@pytest.mark.parametrize("os_type", ["ubuntu", "steamos"])
def test_validate_refuses_an_unsupported_type(tmp_path: Path, os_type: str) -> None:
    with pytest.raises(ValueError, match=f"supports config.type bazzite only; solar is {os_type}"):
        vr_guest.validate(fake_root(tmp_path, os_type), ["solar"])


@pytest.mark.parametrize("ssh_config", ["", "    ssh_config:\n      user: root\n"])
def test_validate_needs_a_desktop_user(tmp_path: Path, ssh_config: str) -> None:
    with pytest.raises(ValueError, match="needs config.ssh_config.user"):
        vr_guest.validate(fake_root(tmp_path, ssh_config=ssh_config), ["solar"])


def test_render_build_maps_every_file_to_its_destination(tmp_path: Path) -> None:
    build_dir, specs = vr_guest.render_build(fake_root(tmp_path), "solar")

    file_map = (build_dir / "file-map.conf").read_text(encoding="utf-8").splitlines()
    assert file_map == [f"{s.build_name}|{s.remote_path}|{s.mode}" for s in specs]
    destinations = {spec.build_name: (spec.remote_path, spec.mode) for spec in specs}
    assert destinations["etc/ssh/sshd_config.d/10-key-only.conf"] == (
        "/etc/ssh/sshd_config.d/10-key-only.conf", "644")
    assert destinations["home/.local/bin/psvr2-steamvr"] == (
        "/var/home/freender/.local/bin/psvr2-steamvr", "755")
    assert destinations["libexec/solar-check"] == ("/usr/local/libexec/solar-check", "755")
    assert destinations["check.json"] == ("/usr/local/share/vr-guest/check.json", "644")
    for spec in specs:
        assert (build_dir / spec.build_name).is_file()


def test_render_build_env(tmp_path: Path) -> None:
    from homelab_install.main import _parse_env_file

    build_dir, _ = vr_guest.render_build(fake_root(tmp_path), "solar")

    assert _parse_env_file(build_dir / "env") == {
        "OS_TYPE": "bazzite",
        "HOSTNAME": "solar",
        "DESKTOP_USER": "freender",
        "DESKTOP_HOME": "/var/home/freender",
        "KARGS": "nvidia-modeset.conceal_vrr_caps=1 bluetooth.disable_ertm=1",
        "NODE_EXPORTER_VERSION": vr_guest.NODE_EXPORTER_VERSION,
        "NODE_EXPORTER_URL": vr_guest.NODE_EXPORTER_URL,
        "NODE_EXPORTER_SHA": vr_guest.NODE_EXPORTER_SHA,
        "CHECK_PATH": "/usr/local/libexec/solar-check",
    }


def test_check_manifest_ids_and_hashes(tmp_path: Path) -> None:
    root = fake_root(tmp_path)
    build_dir, specs = vr_guest.render_build(root, "solar")
    manifest = json.loads((build_dir / "check.json").read_text(encoding="utf-8"))

    assert [entry["id"] for entry in manifest["files"]] == MANIFEST_IDS
    destination = {spec.remote_path: spec.build_name for spec in specs}
    for entry in manifest["files"]:
        built = build_dir / destination[entry["path"]]
        assert entry["sha256"] == hashlib.sha256(built.read_bytes()).hexdigest()
    assert manifest["hostname"] == "solar"
    assert manifest["home"] == "/var/home/freender"
    assert manifest["kargs"] == list(vr_guest.KARGS)
    assert manifest["psvr2tk_zip"] == "/var/home/freender/vr-downloads/PSVR2TK-win64-Ignition.zip"


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


@pytest.fixture
def installer():
    return load_script("vr_guest_installer", INSTALLER_PATH)


def make_ctx(tmp_path: Path, env: dict[str, str] | None = None) -> InstallContext:
    return InstallContext(
        host="solar", script_dir=tmp_path, build_dir=tmp_path, env=env or {},
        deploy_env={}, file_map={}, force_update=False,
    )


class Commands:
    """Stand-in for an installer's `_run`: answers by argv prefix, records every call."""

    def __init__(self, outputs: dict[tuple[str, ...], tuple[int, str]] | None = None):
        self.outputs = outputs or {}
        self.seen: list[list[str]] = []

    def __call__(self, cmd, check=False, **_kwargs):
        argv = list(cmd)
        self.seen.append(argv)
        matches = [v for prefix, v in self.outputs.items() if tuple(argv[:len(prefix)]) == prefix]
        rc, out = matches[0] if matches else (0, "")
        if check and rc:
            raise subprocess.CalledProcessError(rc, argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def test_kargs_appends_only_the_missing_and_never_reboots(installer, tmp_path, capsys) -> None:
    commands = Commands({("rpm-ostree", "kargs"): (0, "rhgb quiet bluetooth.disable_ertm=1\n")})
    installer._run = commands
    ctx = make_ctx(tmp_path, {"KARGS": " ".join(vr_guest.KARGS)})

    installer.ensure_kargs(ctx)

    assert commands.seen == [
        ["rpm-ostree", "kargs"],
        ["rpm-ostree", "kargs", "--append-if-missing=nvidia-modeset.conceal_vrr_caps=1"],
    ]
    assert "reboot required" in capsys.readouterr().out


def test_kargs_already_set(installer, tmp_path, capsys) -> None:
    installer._run = Commands({("rpm-ostree", "kargs"): (0, "a=1 b=2\n")})

    installer.ensure_kargs(make_ctx(tmp_path, {"KARGS": "b=2 a=1"}))

    assert "reboot required" not in capsys.readouterr().out


@pytest.mark.parametrize(("current", "calls"), [("solar\n", 1), ("bazzite\n", 2)])
def test_hostname(installer, current: str, calls: int) -> None:
    commands = Commands({("hostnamectl", "--static"): (0, current)})
    installer._run = commands

    installer.ensure_hostname("solar")

    assert len(commands.seen) == calls
    if calls == 2:
        assert commands.seen[1] == ["hostnamectl", "hostname", "solar"]


@pytest.mark.parametrize(
    ("stdout", "version"),
    [("node_exporter, version 1.12.1 (branch: HEAD)\nbuild user: x\n", "1.12.1"), ("", "")],
)
def test_node_exporter_version(installer, stdout: str, version: str) -> None:
    installer._run = Commands({(installer.NODE_EXPORTER,): (0, stdout)})
    assert installer.node_exporter_version() == version


def test_fetch_verifies_and_drops_a_bad_download(installer, tmp_path, monkeypatch) -> None:
    installer.CACHE_DIR = tmp_path
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"x"))

    good = hashlib.sha256(b"x").hexdigest()
    assert installer.fetch("https://h/a.tar.gz", good).read_bytes() == b"x"
    with pytest.raises(InstallError, match="sha256 mismatch"):
        installer.fetch("https://h/b.tar.gz", "0" * 64)
    assert not (tmp_path / "b.tar.gz").exists()


def test_node_exporter_installs_from_the_pinned_tarball(installer, tmp_path, monkeypatch) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        info = tarfile.TarInfo("node_exporter-9.9.9.linux-amd64/node_exporter")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bin"))
    archive = tmp_path / "ne.tar.gz"
    archive.write_bytes(payload.getvalue())
    installer.NODE_EXPORTER = str(tmp_path / "node_exporter")
    commands = Commands()
    installer._run = commands
    monkeypatch.setattr(installer, "fetch", lambda url, digest: archive)

    assert installer.ensure_node_exporter(make_ctx(tmp_path, {"NODE_EXPORTER_VERSION": "9.9.9"}))
    assert (tmp_path / "node_exporter").read_bytes() == b"bin"
    assert ["restorecon", str(tmp_path / "node_exporter")] in commands.seen


def test_make_user_dirs_creates_only_what_is_missing(installer, tmp_path, monkeypatch) -> None:
    owned: list[Path] = []
    monkeypatch.setattr(installer.os, "chown", lambda path, *_: owned.append(Path(path)))
    (tmp_path / ".config").mkdir()

    installer.make_user_dirs(tmp_path / ".config/systemd/user", tmp_path, 1001, 1001)

    assert owned == [tmp_path / ".config/systemd", tmp_path / ".config/systemd/user"]


def test_session_env_needs_a_bus(installer, tmp_path) -> None:
    assert installer.session_env(999999) is None


@pytest.mark.parametrize(
    ("check_id", "is_owned"),
    [("hostname", True), ("/etc/ssh/sshd_config.d/10-key-only.conf", True),
     ("home/.local/bin/psvr2-audio", True), ("solar_metrics_script", True),
     ("karg:bluetooth.disable_ertm=1", False), ("psvr2toolkit_dll", False),
     ("bluez_build_matches", False)],
)
def test_owned_checks(installer, check_id: str, is_owned: bool) -> None:
    assert installer.owned(check_id) is is_owned


def test_parse_check_keeps_ids_with_colons(installer) -> None:
    output = (
        "  ok    hostname\n"
        "  FAIL  karg:bluetooth.disable_ertm=1: not booted\n"
        "  FAIL  /etc/x.conf: differs from repo\n"
    )
    assert installer.parse_check(output) == [
        ("karg:bluetooth.disable_ertm=1", "not booted"),
        ("/etc/x.conf", "differs from repo"),
    ]


@pytest.mark.parametrize(
    ("rc", "stdout", "error"),
    [
        (0, "  ok    hostname\n", None),
        (1, "  FAIL  psvr2toolkit_dll: not pinned\n", None),
        (1, "  FAIL  sshd: not active\n  FAIL  openxr_runtime: x\n", "failed after deploy: sshd$"),
        (2, "", "exited 2"),
    ],
)
def test_verify(installer, tmp_path, rc: int, stdout: str, error: str | None) -> None:
    commands = Commands({("runuser",): (rc, stdout)})
    installer._run = commands
    ctx = make_ctx(tmp_path, {"CHECK_PATH": "/usr/local/libexec/solar-check"})

    if error:
        with pytest.raises(InstallError, match=error):
            installer.verify(ctx, "freender")
    else:
        installer.verify(ctx, "freender")
    assert commands.seen == [["runuser", "-u", "freender", "--", "/usr/local/libexec/solar-check"]]


def test_install_refuses_another_os(installer, tmp_path) -> None:
    with pytest.raises(InstallError, match="unsupported OS_TYPE 'steamos'"):
        installer.install(make_ctx(tmp_path, {"OS_TYPE": "steamos"}))


# ---------------------------------------------------------------------------
# solar-check
# ---------------------------------------------------------------------------


@pytest.fixture
def check():
    return load_script("solar_check", CHECK_PATH)


def test_check_output_matches_what_solar_metrics_parses(check, tmp_path, capsys) -> None:
    pattern = load_script("solar_metrics", METRICS_PATH).CHECK_LINE
    report = check.Report()
    report.item("karg:a=1", True, "")
    report.item("karg:b=2", False, "not booted")

    lines = capsys.readouterr().out.splitlines()
    assert [pattern.match(line).groups() for line in lines] == [
        ("ok", "karg:a=1"), ("FAIL", "karg:b=2")]
    assert report.failed


def test_check_reports_every_id(check, tmp_path, monkeypatch, capsys) -> None:
    root = fake_root(tmp_path)
    build_dir, _ = vr_guest.render_build(root, "solar")
    monkeypatch.setattr(check, "output", lambda *cmd: "")
    monkeypatch.setattr(check, "succeeds", lambda *cmd: False)

    assert check.main(build_dir / "check.json") == 1

    ids = [re.split(r"\s+", line.strip())[1].rstrip(":")
           for line in capsys.readouterr().out.splitlines()]
    assert sorted(ids) == sorted(CHECK_IDS)


def test_check_file_entries_compare_hashes(check, tmp_path, capsys) -> None:
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_text("a")
    bad.write_text("b")
    manifest = {"hostname": "solar", "kargs": [], "files": [
        {"id": "good", "path": str(good), "sha256": hashlib.sha256(b"a").hexdigest()},
        {"id": "bad", "path": str(bad), "sha256": hashlib.sha256(b"a").hexdigest()},
        {"id": "gone", "path": str(tmp_path / "gone"), "sha256": "x"},
    ]}
    check.check_system(check.Report(), manifest)

    out = capsys.readouterr().out
    assert "  ok    good\n" in out
    assert "  FAIL  bad: differs from repo\n" in out
    assert "  FAIL  gone: differs from repo\n" in out


@pytest.mark.parametrize(
    ("settings", "passes"),
    [
        ({"steamvr": {"enableLinuxVulkanAsync": True, "useFacetRenderer": True,
                      "showMirrorView": False}}, True),
        ({"steamvr": {"enableLinuxVulkanAsync": True, "useFacetRenderer": True}}, False),
        ({"steamvr": {"enableLinuxVulkanAsync": False, "useFacetRenderer": True,
                      "showMirrorView": False}}, False),
        ([], False),
    ],
)
def test_steamvr_settings(check, tmp_path, settings, passes: bool) -> None:
    path = tmp_path / "steamvr.vrsettings"
    path.write_text(json.dumps(settings))
    assert check.steamvr_settings_ok(path) is passes
    assert check.steamvr_settings_ok(tmp_path / "missing") is False


def test_zip_member(check, tmp_path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("driver_playstation_vr2.dll", b"dll")
    assert check.zip_member(archive, "driver_playstation_vr2.dll") == b"dll"
    assert check.zip_member(archive, "other.dll") is None
    assert check.zip_member(tmp_path / "missing.zip", "x") is None


@pytest.mark.parametrize(
    ("rpm", "marker", "passes"),
    [
        ("terra-bluez-5.87-2.fc45.x86_64", "terra-bluez-5.87-2.fc45.x86_64", True),
        ("terra-bluez-5.88-1.fc45.x86_64", "terra-bluez-5.87-2.fc45.x86_64", False),
        ("package terra-bluez is not installed", None, False),
    ],
)
def test_bluez_build_matches(check, tmp_path, monkeypatch, capsys, rpm, marker, passes) -> None:
    check.BLUEZ_MARKER = tmp_path / "built-for"
    if marker:
        check.BLUEZ_MARKER.write_text(marker + "\n")
    monkeypatch.setattr(check, "output", lambda *cmd: rpm if cmd[0] == "rpm" else "")
    report = check.Report()

    check.check_bluez(report)

    line = capsys.readouterr().out.splitlines()[1]
    status = "ok  " if passes else "FAIL"
    assert line.startswith(f"  {status}  bluez_build_matches")


def test_check_unreadable_manifest(check, tmp_path) -> None:
    assert check.main(tmp_path / "missing.json") == 2
