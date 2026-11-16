"""vr-guest: solar's guest-side setup (freender/homelab-ops#400, #401).

Three parts, three loaders: the orchestrator is a package module; the remote
installer and `solar-check` are scripts loaded by path. The check ids are pinned
literally because they are the `check` label of homelab_solar_check: renaming one
silently splits its history and the SolarPatchCheckFailed series in solar.yml.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from homelab.modules import vector_host, vr_guest
from homelab_install import systemd, vector
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
    "/etc/udev/rules.d/50-goxlr.rules",
    "/etc/systemd/system/node_exporter.service",
    "/etc/systemd/system/solar-metrics.service",
    "/etc/systemd/system/solar-metrics.timer",
    "/etc/systemd/system/solar-gpu-metrics.service",
    "/etc/systemd/system/vector.service",
    "home/.local/bin/psvr2-steamvr",
    "home/.local/bin/psvr2-audio",
    "home/.config/systemd/user/psvr2-steamvr.service",
    "home/.config/autostart/goxlr-daemon.desktop",
    "home/.local/share/applications/goxlr-utility.desktop",
    "solar_metrics_script",
    "solar_gpu_metrics_script",
    "/etc/vector/homelab.yaml",
    "vector_wrapper",
    "bluez_dropin",
]
CHECK_IDS = [
    "hostname", "karg:nvidia-modeset.conceal_vrr_caps=1", "karg:bluetooth.disable_ertm=1",
    "conceal_vrr_caps", *MANIFEST_IDS, "steam_library", "sshd", "steamvr_linux_fixes",
    "ignition_shim", "psvr2toolkit_dll", "openvr_driver", "compositor_cap_sys_nice",
    "vrmonitor_handler", "openxr_runtime", "steamvr_settings", "kde_ddc_ci", "patched_bluetoothd",
    "bluez_build_matches", "bluez_build_inputs", "node_exporter_version", "solar_metrics_timer",
    "solar_gpu_metrics_active", "vector_version", "vector_active", "goxlr_version",
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
LOGS = """logs:
  config:
    type: lxc
    hostname: logs.internal
    user: root
    sshkey: infra
  features:
    docker-stacks:
      stacks:
        - victoria-logs
"""


def fake_root(tmp_path: Path, os_type: str = "bazzite", ssh_config: str = DESKTOP) -> Path:
    (tmp_path / "hosts.conf").write_text(
        SOLAR.format(type=os_type, ssh_config=ssh_config) + LOGS, encoding="utf-8"
    )
    shutil.copytree(ROOT / "vr-guest", tmp_path / "vr-guest",
                    ignore=shutil.ignore_patterns("build", "__pycache__"))
    for module in ("vector-host", "vector-config"):
        for sub in ("templates", "configs"):
            shutil.copytree(ROOT / module / sub, tmp_path / module / sub)
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
    assert destinations["vector/vector.yaml"] == ("/etc/vector/homelab.yaml", "644")
    assert destinations["vector/journalctl-catchup"] == (
        "/usr/local/bin/journalctl-catchup", "755")
    assert (build_dir / "vector/vector.test.yaml").is_file()
    assert destinations["bluez/50-eagain-patch.conf"] == (
        "/etc/systemd/system/bluetooth.service.d/50-eagain-patch.conf", "644")
    assert (build_dir / "bluez/build.sh").is_file()
    assert sorted(p.name for p in (build_dir / "bluez/patches").iterdir()) == [
        p.name for p in vr_guest.bluez_patches(ROOT)]
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
        "VECTOR_VERSION": "0.61.0",
        "VECTOR_URL": "https://github.com/vectordotdev/vector/releases/download/v0.61.0/"
                      "vector-0.61.0-x86_64-unknown-linux-gnu.tar.gz",
        "VECTOR_SHA": vr_guest.VECTOR_SHA,
        "VECTOR_QUERY_URL": "http://logs.internal:9428/select/logsql/query",
        "VECTOR_TENANT": "0",
        "GOXLR_VERSION": "1.2.4",
        "GOXLR_URL": "https://github.com/GoXLR-on-Linux/goxlr-utility/releases/download/"
                     "v1.2.4/goxlr-utility-1.2.4-1.x86_64.rpm",
        "GOXLR_SHA": vr_guest.GOXLR_SHA,
        "BLUEZ_TERRA": "terra-bluez-5.87+1.git8750129efca8-2.fc45.x86_64",
        "BLUEZ_IMAGE": "registry.fedoraproject.org/fedora:45",
        "BLUEZ_INPUTS": vr_guest.bluez_inputs(ROOT),
        "SVLF_URL": "https://github.com/BnuuySolutions/SteamVRLinuxFixes/releases/download/"
                    "v0.2.1/VK_LAYER_BNUUY_steamvr_linux_fixes.zip",
        "SVLF_SHA": vr_guest.VR_RELEASES["SVLF"][3],
        "IGNITION_URL": "https://github.com/BnuuySolutions/Ignition/releases/download/"
                        "v1.1.0/Ignition-Linux-Windows.zip",
        "IGNITION_SHA": vr_guest.VR_RELEASES["IGNITION"][3],
        "PSVR2TK_URL": "https://github.com/BnuuySolutions/PSVR2Toolkit/releases/download/"
                       "v1.0.0-experimental-3/PSVR2TK-win64-Ignition.zip",
        "PSVR2TK_SHA": vr_guest.VR_RELEASES["PSVR2TK"][3],
        "UNITYSETUP_URL": "https://github.com/BnuuySolutions/PSVR2Toolkit.UnitySetup/releases/"
                          "download/v1.1.0/PSVR2Toolkit.UnitySetup-Linux.zip",
        "UNITYSETUP_SHA": vr_guest.VR_RELEASES["UNITYSETUP"][3],
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
    assert manifest["vector_version"] == vr_guest.VECTOR_VERSION
    assert manifest["goxlr_version"] == vr_guest.GOXLR_VERSION
    assert manifest["psvr2toolkit_dll_sha256"] == vr_guest.PSVR2TK_DLL_SHA
    assert manifest["bluez_inputs"] == vr_guest.bluez_inputs(root)
    assert manifest["steamvr_settings"] == {
        "enableLinuxVulkanAsync": True, "useFacetRenderer": True, "showMirrorView": False,
        "supersampleManualOverride": True, "supersampleScale": 1.18,
    }


def test_vector_config_is_vector_hosts_render_at_tenant_0(tmp_path: Path) -> None:
    """One config for every host-native Vector (#402): vector-host renders it, solar
    always writes to tenant 0 with the journal wrapper vector-config ships."""
    root = fake_root(tmp_path)
    build_dir, _ = vr_guest.render_build(root, "solar")
    reference = tmp_path / "reference"
    vector_host.render_config(root, "solar", reference, shadow=False,
                              steam_logs=vr_guest.steam_log_paths("/var/home/freender"))

    for name in ("vector.yaml", "vector.test.yaml", "journalctl-catchup"):
        assert (build_dir / "vector" / name).read_bytes() == (reference / name).read_bytes()
    config = yaml.safe_load((build_dir / "vector/vector.yaml").read_text(encoding="utf-8"))
    for sink in (config["sinks"]["vlogs_journald"], config["sinks"]["vlogs_steam"]):
        assert sink["endpoints"] == ["http://logs.internal:9428/insert/elasticsearch/"]
        assert "request" not in sink
    assert config["sources"]["journald"]["journalctl_path"] == "/usr/local/bin/journalctl-catchup"


def test_vector_ships_the_steam_logs_the_skill_debugs_from(tmp_path: Path) -> None:
    build_dir, _ = vr_guest.render_build(fake_root(tmp_path), "solar")
    config = yaml.safe_load((build_dir / "vector/vector.yaml").read_text(encoding="utf-8"))

    source = config["sources"]["steam_logs"]
    logs = "/var/home/freender/.local/share/Steam/logs"
    assert source["include"] == [f"{logs}/{name}" for name in (
        "vrserver.txt", "vrcompositor.txt", "vrcompositor-linux.txt",
        "console_log.txt", "connection_log.txt", "webhelper.txt", "webhelper_vr.txt")]
    assert source["read_from"] == "end"
    assert source["fingerprint"] == {"strategy": "device_and_inode"}
    sink = config["sinks"]["vlogs_steam"]
    assert sink["inputs"] == ["steam_shape"]
    assert sink["query"]["_stream_fields"] == "_HOSTNAME,steam_log"
    assert sink["acknowledgements"] == {"enabled": True}
    assert sink["buffer"]["type"] == "disk"


def test_validate_needs_victoria_logs(tmp_path: Path) -> None:
    root = fake_root(tmp_path)
    (root / "hosts.conf").write_text(SOLAR.format(type="bazzite", ssh_config=DESKTOP))

    with pytest.raises(ValueError, match="exactly one host running the victoria-logs stack"):
        vr_guest.validate(root, ["solar"])


def test_vector_host_refuses_a_vr_guest(tmp_path: Path) -> None:
    root = fake_root(tmp_path)
    hosts = (root / "hosts.conf").read_text()
    (root / "hosts.conf").write_text(
        hosts.replace("    vr-guest:\n", "    vr-guest:\n    vector-host:\n"))
    (root / "vector-host" / "scripts").mkdir()
    (root / "vector-host" / "scripts" / "install.py").write_text("")

    with pytest.raises(ValueError, match="solar: vr-guest installs its Vector, not vector-host"):
        vector_host.validate(root, ["solar"])


def test_the_unit_runs_vector_without_a_user_of_its_own() -> None:
    unit = (ROOT / "vr-guest/configs/etc/systemd/system/vector.service").read_text()
    lines = unit.splitlines()

    for line in ("DynamicUser=yes", "SupplementaryGroups=systemd-journal", "StateDirectory=vector",
                 "Environment=VECTOR_CONFIG=/etc/vector/homelab.yaml", "StartLimitIntervalSec=0",
                 "RestartSec=30s", "ExecStart=/usr/local/bin/vector",
                 "ExecStartPre=/usr/local/bin/vector validate --skip-healthchecks",
                 "AmbientCapabilities=CAP_DAC_READ_SEARCH",
                 "CapabilityBoundingSet=CAP_DAC_READ_SEARCH"):
        assert line in lines
    assert lines.index("StartLimitIntervalSec=0") < lines.index("[Service]")


def test_real_solar_render_passes_vector(tmp_path: Path) -> None:
    binary = os.environ.get("VECTOR_BIN") or shutil.which("vector")
    cached = Path.home() / f".cache/vector-sync/{vr_guest.VECTOR_VERSION}/vector"
    binary = binary or (str(cached) if cached.is_file() else None)
    if not binary:
        pytest.skip("no vector binary (VECTOR_BIN or PATH)")
    (tmp_path / "repo").mkdir()
    root = fake_root(tmp_path / "repo")
    shutil.copy(ROOT / "hosts.conf", root / "hosts.conf")
    out = vr_guest.render_build(root, "solar")[0] / "vector"
    for args in (["validate", "--no-environment", str(out / "vector.yaml")],
                 ["test", str(out / "vector.yaml"), str(out / "vector.test.yaml")]):
        result = subprocess.run([binary, *args], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr


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


def test_wayland_display_skips_lock_files(installer, tmp_path) -> None:
    assert installer.wayland_display(str(tmp_path)) is None
    (tmp_path / "wayland-0.lock").touch()
    (tmp_path / "wayland-1").touch()
    assert installer.wayland_display(str(tmp_path)) == "wayland-1"


def test_ddc_ci_changes_disallow_before_brightness(installer) -> None:
    outputs = [
        {"name": "HDMI-A-1", "connected": True, "ddcCiAllowed": True, "brightness": 0.1111},
        {"name": "DP-1", "connected": False, "ddcCiAllowed": True, "brightness": 0.5},
        {"name": "DP-3", "connected": True, "ddcCiAllowed": False, "brightness": 1},
    ]
    assert installer.ddc_ci_changes(outputs) == [
        "output.HDMI-A-1.ddcCi.disallow", "output.HDMI-A-1.brightness.100"]
    assert installer.ddc_ci_changes(outputs[2:]) == []


@pytest.mark.parametrize(
    ("doctor", "applied"),
    [('{"outputs": [{"name": "HDMI-A-1", "connected": true, "ddcCiAllowed": true,'
      ' "brightness": 1}]}', True),
     ('{"outputs": [{"name": "HDMI-A-1", "connected": true, "ddcCiAllowed": false,'
      ' "brightness": 1}]}', False)],
)
def test_ensure_no_ddc_ci(installer, tmp_path, doctor: str, applied: bool) -> None:
    (tmp_path / "wayland-1").touch()
    session = {"XDG_RUNTIME_DIR": str(tmp_path)}
    commands = Commands({("runuser",): (0, doctor)})
    installer._run = commands

    assert installer.ensure_no_ddc_ci("freender", session)

    prefix = ["runuser", "-u", "freender", "--", "env", f"XDG_RUNTIME_DIR={tmp_path}",
              "WAYLAND_DISPLAY=wayland-1", "kscreen-doctor"]
    expected = [[*prefix, "-j"]]
    if applied:
        expected.append([*prefix, "output.HDMI-A-1.ddcCi.disallow"])
    assert commands.seen == expected


def test_ensure_no_ddc_ci_without_wayland_is_not_owned(installer, tmp_path) -> None:
    installer._run = Commands()
    assert not installer.ensure_no_ddc_ci("freender", {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert installer._run.seen == []


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


def test_verify_owns_the_vr_ids_the_vr_stage_converged(installer, tmp_path) -> None:
    installer._run = Commands({("runuser",): (1, "  FAIL  psvr2toolkit_dll: not pinned\n")})
    ctx = make_ctx(tmp_path, {"CHECK_PATH": "/c"})

    with pytest.raises(InstallError, match="failed after deploy: psvr2toolkit_dll$"):
        installer.verify(ctx, "freender", frozenset({"psvr2toolkit_dll"}))


# --- vector stage ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "version"),
    [("vector 0.61.0 (x86_64-unknown-linux-gnu 17e017e 2026-10-29)\n", "0.61.0"),
     ("", ""), ("bash: vector: not found\n", "")],
)
def test_vector_version(installer, stdout: str, version: str) -> None:
    installer._run = Commands({(installer.VECTOR,): (0, stdout)})
    assert installer.vector_version() == version


def vector_tarball(tmp_path: Path, member: str) -> Path:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bin"))
    archive = tmp_path / "vector.tar.gz"
    archive.write_bytes(payload.getvalue())
    return archive


def test_vector_installs_from_the_pinned_tarball(installer, tmp_path, monkeypatch) -> None:
    """Upstream's tarball names members with a leading `./`."""
    archive = vector_tarball(tmp_path, "./vector-x86_64-unknown-linux-gnu/bin/vector")
    installer.VECTOR = str(tmp_path / "vector")
    commands = Commands()
    installer._run = commands
    monkeypatch.setattr(installer, "fetch", lambda url, digest: archive)

    assert installer.ensure_vector_binary(make_ctx(tmp_path, {"VECTOR_VERSION": "0.61.0"}))
    assert (tmp_path / "vector").read_bytes() == b"bin"
    assert os.access(tmp_path / "vector", os.X_OK)
    assert ["restorecon", str(tmp_path / "vector")] in commands.seen


def test_vector_tarball_without_the_binary_is_refused(installer, tmp_path, monkeypatch) -> None:
    archive = vector_tarball(tmp_path, "./vector-x86_64-unknown-linux-gnu/README.md")
    installer.VECTOR = str(tmp_path / "vector")
    installer._run = Commands()
    monkeypatch.setattr(installer, "fetch", lambda url, digest: archive)

    with pytest.raises(InstallError, match="has no vector-x86_64-unknown-linux-gnu/bin/vector"):
        installer.ensure_vector_binary(make_ctx(tmp_path, {"VECTOR_VERSION": "0.61.0"}))
    assert not (tmp_path / "vector").exists()


def test_the_pinned_vector_is_left_alone(installer, tmp_path, monkeypatch) -> None:
    installer._run = Commands({(installer.VECTOR,): (0, "vector 0.61.0 (x86_64)\n")})
    monkeypatch.setattr(installer, "fetch", lambda *_: pytest.fail("downloaded"))

    assert not installer.ensure_vector_binary(make_ctx(tmp_path, {"VECTOR_VERSION": "0.61.0"}))


VECTOR_ENV = {
    "VECTOR_VERSION": "0.61.0", "VECTOR_URL": "https://h/v.tar.gz", "VECTOR_SHA": "x",
    "VECTOR_QUERY_URL": "http://logs:9428/select/logsql/query", "VECTOR_TENANT": "0",
}
VECTOR_STAGED = ("vector/journalctl-catchup", "vector/vector.yaml",
                 "etc/systemd/system/vector.service")


def vector_stage(installer, tmp_path: Path, monkeypatch, commands: Commands,
                 live: dict[str, str] | None = None, delivered: bool = True):
    for module in (installer, systemd, vector):
        monkeypatch.setattr(module, "_run", commands)
    monkeypatch.setattr(installer, "ensure_vector_binary", lambda ctx: False)
    monkeypatch.setattr(vector, "_sleep", lambda _s: None)
    monkeypatch.setattr(vector, "_nonce", lambda: "nonce")
    answer = b'{"_msg":"delivery check nonce"}\n' if delivered else b""
    monkeypatch.setattr(vector, "_urlopen", lambda *_a, **_k: io.BytesIO(answer))
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(vector, "_now", lambda: float(next(clock)))
    build = tmp_path / "build"
    file_map = {}
    for name in (*VECTOR_STAGED, "vector/vector.test.yaml"):
        (build / name).parent.mkdir(parents=True, exist_ok=True)
        (build / name).write_text(f"{name} new\n")
    for name in VECTOR_STAGED:
        dest = tmp_path / "live" / Path(name).name
        file_map[name] = (str(dest), "644")
        if live and name in live:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(live[name])
    return InstallContext(host="solar", script_dir=tmp_path, build_dir=build, env=VECTOR_ENV,
                          deploy_env={}, file_map=file_map, force_update=False)


VECTOR_CONVERGED = {name: f"{name} new\n" for name in VECTOR_STAGED}


class StoppedVector(Commands):
    """A disabled, stopped vector.service that `enable --now` or `start` brings up."""

    def __init__(self, outputs=None, enabled: bool = False):
        super().__init__(outputs)
        self.enabled, self.active = enabled, False

    def __call__(self, cmd, check=False, **kwargs):
        argv = list(cmd)
        if argv[:1] == ["systemctl"] and argv[1] in ("is-active", "is-enabled"):
            self.seen.append(argv)
            up = self.active if argv[1] == "is-active" else self.enabled
            return subprocess.CompletedProcess(argv, 0 if up else 3, stdout="", stderr="")
        if argv[:1] == ["systemctl"] and argv[1] in ("enable", "start", "restart"):
            self.enabled = self.active = True
        return super().__call__(cmd, check=check, **kwargs)


def first(commands: Commands, *prefix: str) -> int:
    return next(i for i, argv in enumerate(commands.seen) if argv[:len(prefix)] == list(prefix))


def test_vector_stage_on_a_fresh_host(installer, tmp_path, monkeypatch) -> None:
    commands = StoppedVector()
    ctx = vector_stage(installer, tmp_path, monkeypatch, commands)

    installer.install_vector(ctx)

    for name in VECTOR_STAGED:
        assert Path(ctx.file_map[name][0]).read_text() == f"{name} new\n"
    wrapper = ctx.file_map["vector/journalctl-catchup"][0]
    assert first(commands, "restorecon", wrapper) < first(commands, installer.VECTOR, "validate")
    assert first(commands, installer.VECTOR, "test") < first(commands, "systemctl", "daemon-reload")
    assert ["systemctl", "enable", "--now", "vector.service"] in commands.seen
    assert ["logger", "-t", "vr-guest", "delivery check nonce"] in commands.seen


def test_vector_stage_leaves_a_converged_running_host_alone(installer, tmp_path,
                                                            monkeypatch) -> None:
    commands = Commands()
    ctx = vector_stage(installer, tmp_path, monkeypatch, commands, live=VECTOR_CONVERGED)

    installer.install_vector(ctx)

    assert [installer.VECTOR, "validate", "--no-environment",
            str(ctx.build_dir / "vector/vector.yaml")] in commands.seen
    assert not any(argv[:2] == ["systemctl", "restart"] for argv in commands.seen)
    assert not any(argv[0] in ("logger", "restorecon") for argv in commands.seen)


@pytest.mark.parametrize("changed", VECTOR_STAGED)
def test_vector_stage_restarts_on_any_changed_file(installer, tmp_path, monkeypatch,
                                                    changed: str) -> None:
    commands = Commands()
    live = dict(VECTOR_CONVERGED, **{changed: "old\n"})
    ctx = vector_stage(installer, tmp_path, monkeypatch, commands, live=live)

    installer.install_vector(ctx)

    assert first(commands, "systemctl", "daemon-reload") < first(commands, "systemctl", "restart")
    assert any(argv[0] == "logger" for argv in commands.seen)


def test_vector_stage_a_failed_check_installs_no_config(installer, tmp_path, monkeypatch) -> None:
    commands = Commands({(installer.VECTOR, "test"): (1, "")})
    live = dict(VECTOR_CONVERGED, **{"vector/vector.yaml": "old\n"})
    ctx = vector_stage(installer, tmp_path, monkeypatch, commands, live=live)

    with pytest.raises(InstallError, match="vector unit tests failed"):
        installer.install_vector(ctx)

    assert Path(ctx.file_map["vector/vector.yaml"][0]).read_text() == "old\n"
    assert not any(argv[:2] == ["systemctl", "restart"] for argv in commands.seen)


def test_vector_stage_fails_when_nothing_arrives(installer, tmp_path, monkeypatch) -> None:
    commands = StoppedVector(enabled=True)
    ctx = vector_stage(installer, tmp_path, monkeypatch, commands, live=VECTOR_CONVERGED,
                       delivered=False)

    with pytest.raises(InstallError, match="vector.service delivered nothing within 30s"):
        installer.install_vector(ctx)


@pytest.mark.parametrize("missing", list(VECTOR_ENV))
def test_vector_stage_refuses_an_incomplete_env(installer, tmp_path, monkeypatch,
                                                missing: str) -> None:
    commands = Commands()
    ctx = dataclasses.replace(
        vector_stage(installer, tmp_path, monkeypatch, commands),
        env={key: value for key, value in VECTOR_ENV.items() if key != missing},
    )

    with pytest.raises(InstallError, match=missing):
        installer.install_vector(ctx)
    assert commands.seen == []


# --- vr stage ----------------------------------------------------------------------


@pytest.fixture
def desktop(installer, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return installer.Desktop("freender", home, os.getuid(), os.getgid())


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as handle:
        for name, data in members.items():
            handle.writestr(name, data)
    return path


def test_put_writes_only_on_change_and_sets_the_mode(installer, desktop) -> None:
    dest = desktop.home / ".local/lib/x.so"

    assert installer.put(desktop, dest, b"a", 0o755)
    assert dest.read_bytes() == b"a"
    assert dest.stat().st_mode & 0o777 == 0o755
    assert not installer.put(desktop, dest, b"a", 0o644)
    assert dest.stat().st_mode & 0o777 == 0o644
    assert installer.put(desktop, dest, b"b", 0o644)
    assert sorted(p.name for p in dest.parent.iterdir()) == ["x.so"]


def test_put_replaces_a_symlink(installer, desktop) -> None:
    target = desktop.home / "target"
    target.write_bytes(b"a")
    dest = desktop.home / "link"
    dest.symlink_to(target)

    assert installer.put(desktop, dest, b"a", 0o644)
    assert not dest.is_symlink()


def test_ensure_link(installer, desktop) -> None:
    link = desktop.home / ".config/openxr/1/active_runtime.json"
    target = desktop.home / "steamxr_linux64.json"

    assert installer.ensure_link(desktop, link, target)
    assert not installer.ensure_link(desktop, link, target)
    assert installer.ensure_link(desktop, link, desktop.home / "other.json")
    assert link.readlink() == desktop.home / "other.json"


def test_extract_skips_unchanged_members_and_sets_modes(installer, desktop, tmp_path) -> None:
    archive = make_zip(tmp_path / "a.zip", {
        "run.sh": b"#!/bin/sh", "proton": b"p", "lib.dll": b"d", "Data/": b"", "Data/x": b"x",
    })
    dest = desktop.home / "ignition"

    assert installer.extract(desktop, archive, dest, frozenset({"proton"})) == 4
    modes = {name: (dest / name).stat().st_mode & 0o777
             for name in ("run.sh", "proton", "lib.dll", "Data/x")}
    assert modes == {"run.sh": 0o755, "proton": 0o755, "lib.dll": 0o644, "Data/x": 0o644}
    (dest / "lib.dll").chmod(0o600)
    assert installer.extract(desktop, archive, dest, frozenset({"proton"})) == 0
    assert (dest / "lib.dll").stat().st_mode & 0o777 == 0o644
    (dest / "Data/x").write_bytes(b"y")
    assert installer.extract(desktop, archive, dest, frozenset({"proton"})) == 1
    assert (dest / "Data/x").read_bytes() == b"x"


@pytest.mark.parametrize("name", ["../evil", "a/../../evil"])
def test_extract_refuses_to_leave_dest(installer, desktop, tmp_path, name: str) -> None:
    archive = make_zip(tmp_path / "a.zip", {name: b"x"})
    with pytest.raises(InstallError, match="unsafe member"):
        installer.extract(desktop, archive, desktop.home / "d")


def fake_release(installer, monkeypatch, archives: dict[str, Path]) -> None:
    monkeypatch.setattr(installer, "fetch_release", lambda ctx, prefix: archives[prefix])


def test_install_svlf_puts_the_layer_under_local(installer, desktop, tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "svlf.zip", {
        "install.sh": b"", "libsteamvr_linux_fixes.so": b"so",
        "VkLayer_steamvr_linux_fixes.json": b"{}",
    })
    fake_release(installer, monkeypatch, {"SVLF": archive})

    installer.install_svlf(make_ctx(tmp_path), desktop)

    lib = desktop.home / ".local/lib/libsteamvr_linux_fixes.so"
    layer = desktop.home / ".local/share/vulkan/implicit_layer.d/VkLayer_steamvr_linux_fixes.json"
    assert lib.read_bytes() == b"so" and lib.stat().st_mode & 0o777 == 0o755
    assert layer.read_bytes() == b"{}" and layer.stat().st_mode & 0o777 == 0o644
    assert not (desktop.home / ".local/lib/install.sh").exists()


def make_plugin(home: Path, installer) -> Path:
    plugin = home / installer.PLUGIN
    (plugin / "bin/win64").mkdir(parents=True)
    (plugin / "driver.vrdrivermanifest").write_text('{"name": "playstation_vr2"}')
    return plugin


def test_install_ignition_does_what_install_ignition_sh_does(installer, desktop, tmp_path,
                                                             monkeypatch) -> None:
    names = ["libdriver_ignition.so", "ignition_server.exe", *installer.IGNITION_SUPPORT]
    archive = make_zip(tmp_path / "ign.zip", {name: name.encode() for name in names})
    fake_release(installer, monkeypatch, {"IGNITION": archive})
    plugin = make_plugin(desktop.home, installer)

    installer.install_ignition(make_ctx(tmp_path), desktop, plugin)

    ignition, linux64 = desktop.home / "ignition", plugin / "bin/linux64"
    shim = linux64 / "driver_playstation_vr2.so"
    assert shim.readlink() == ignition / "libdriver_ignition.so"
    assert json.loads((linux64 / "ignition.json").read_text()) == {
        "server_exe": f"{ignition}/ignition_server.exe",
        "driver_dll": "../win64/driver_playstation_vr2.dll",
        "wine_cmd": ["./launch_serverhelper.sh"],
        "wait_for_debugger": False,
    }
    for name in installer.IGNITION_SUPPORT:
        assert (linux64 / name).read_bytes() == name.encode()
    assert (linux64 / "proton").stat().st_mode & 0o777 == 0o755
    assert (linux64 / "wine_hidraw.reg").stat().st_mode & 0o777 == 0o644


def write_vrpath(home: Path, drivers) -> None:
    path = home / ".config/openvr/openvrpaths.vrpath"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"external_drivers": drivers}))


def test_driver_registered_follows_symlinked_steam_paths(installer, desktop) -> None:
    plugin = make_plugin(desktop.home, installer)
    assert not installer.driver_registered(desktop, plugin)
    (desktop.home / ".steam").mkdir()
    (desktop.home / ".steam/steam").symlink_to(desktop.home / ".local/share/Steam")
    write_vrpath(desktop.home, None)
    assert not installer.driver_registered(desktop, plugin)
    registered = ".steam/steam/steamapps/common/PlayStation VR2 App/SteamVR_Plug-In"
    write_vrpath(desktop.home, [str(desktop.home / registered)])
    assert installer.driver_registered(desktop, plugin)


def test_register_driver_runs_vrpathreg_only_when_missing(installer, desktop, tmp_path) -> None:
    plugin = make_plugin(desktop.home, installer)
    steamvr = desktop.home / installer.STEAMVR
    commands = Commands()
    installer._run = commands

    installer.register_driver(desktop, steamvr, plugin)
    write_vrpath(desktop.home, [str(plugin)])
    installer.register_driver(desktop, steamvr, plugin)

    assert commands.seen == [["runuser", "-u", "freender", "--", "env",
                              f"{steamvr}/bin/vrpathreg.sh", "adddriver", str(plugin)]]


TOOLKIT = b"toolkit PlayStation VR2 Toolkit - v1"
TOOLKIT_SHA = hashlib.sha256(TOOLKIT).hexdigest()


@pytest.mark.parametrize(
    ("current", "kept", "expect_kept"),
    [
        (b"sony", None, b"sony"),            # first install
        (b"sony v2", b"sony", b"sony v2"),   # PS VR2 App update restored a newer Sony dll
        (TOOLKIT, b"sony", b"sony"),         # already in place
        (b"older PlayStation VR2 Toolkit", b"sony", b"sony"),  # an older pinned toolkit
        (None, b"sony", b"sony"),
    ],
)
def test_psvr2toolkit_keeps_sonys_dll(installer, desktop, tmp_path, monkeypatch,
                                      current, kept, expect_kept) -> None:
    plugin = make_plugin(desktop.home, installer)
    win64 = plugin / "bin/win64"
    if current is not None:
        (win64 / installer.SONY_DLL).write_bytes(current)
    if kept is not None:
        (win64 / installer.SONY_DLL_KEPT).write_bytes(kept)
    archive = make_zip(tmp_path / "tk.zip", {installer.SONY_DLL: TOOLKIT, "capi.dll": b"c"})
    fake_release(installer, monkeypatch, {"PSVR2TK": archive})

    installer.install_psvr2toolkit(make_ctx(tmp_path), desktop, plugin, TOOLKIT_SHA)

    assert (win64 / installer.SONY_DLL_KEPT).read_bytes() == expect_kept
    assert (win64 / installer.SONY_DLL).read_bytes() == TOOLKIT
    assert (win64 / "capi.dll").read_bytes() == b"c"


def test_psvr2toolkit_refuses_without_sonys_dll(installer, desktop, tmp_path) -> None:
    plugin = make_plugin(desktop.home, installer)
    with pytest.raises(InstallError, match="verify the PS VR2 App"):
        installer.install_psvr2toolkit(make_ctx(tmp_path), desktop, plugin, TOOLKIT_SHA)


@pytest.mark.parametrize(("caps", "set_it"), [("x cap_sys_nice=eip\n", False), ("", True)])
def test_cap_sys_nice(installer, tmp_path, caps: str, set_it: bool) -> None:
    commands = Commands({("getcap",): (0, caps)})
    installer._run = commands

    installer.ensure_cap_sys_nice(tmp_path)

    launcher = f"{tmp_path}/bin/linux64/vrcompositor-launcher"
    assert (["setcap", "CAP_SYS_NICE=eip", launcher] in commands.seen) is set_it


def test_scheme_handlers_wait_for_steamvrs_desktop_files(installer, desktop) -> None:
    commands = Commands()
    installer._run = commands
    assert installer.ensure_scheme_handlers(desktop) is False
    assert commands.seen == []


def test_scheme_handlers_set_only_what_is_missing(installer, desktop) -> None:
    applications = desktop.home / ".local/share/applications"
    applications.mkdir(parents=True)
    (applications / "valve-vrmonitor.desktop").write_text("")
    commands = Commands({
        ("runuser", "-u", "freender", "--", "xdg-mime", "query", "default",
         "x-scheme-handler/vrmonitor"): (0, "valve-vrmonitor.desktop\n"),
    })
    installer._run = commands

    assert installer.ensure_scheme_handlers(desktop)

    writes = [argv[5:] for argv in commands.seen if "query" not in argv]
    assert writes == [["update-desktop-database", str(applications)],
                      ["xdg-mime", "default", "valve-steamvr.desktop", "x-scheme-handler/steamvr"]]


def settings_path(desktop, installer) -> Path:
    path = desktop.home / installer.STEAMVR_SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_steamvr_settings_merge_and_back_up(installer, desktop, monkeypatch) -> None:
    path = settings_path(desktop, installer)
    path.write_text('{"steamvr": {"installID": "1", "showMirrorView": true}, "other": {}}')
    monkeypatch.setattr(installer, "steamvr_running", lambda: False)

    assert installer.ensure_steamvr_settings(desktop, WANTED)

    assert json.loads(path.read_text()) == {
        "steamvr": {"installID": "1", "showMirrorView": False, "supersampleScale": 1.18},
        "other": {},
    }
    assert len(list(path.parent.glob("steamvr.vrsettings.bak.*"))) == 1
    assert installer.ensure_steamvr_settings(desktop, WANTED)
    assert len(list(path.parent.glob("steamvr.vrsettings.bak.*"))) == 1


def test_steamvr_settings_created_when_missing(installer, desktop, monkeypatch) -> None:
    path = settings_path(desktop, installer)
    monkeypatch.setattr(installer, "steamvr_running", lambda: False)

    assert installer.ensure_steamvr_settings(desktop, WANTED)
    assert json.loads(path.read_text()) == {"steamvr": WANTED}


def test_steamvr_settings_left_alone_once_steamvr_starts(installer, desktop, monkeypatch) -> None:
    path = settings_path(desktop, installer)
    path.write_text("{}")
    monkeypatch.setattr(installer, "steamvr_running", lambda: True)

    assert installer.ensure_steamvr_settings(desktop, WANTED) is False
    assert path.read_text() == "{}"


def test_steamvr_settings_refuse_broken_json(installer, desktop) -> None:
    settings_path(desktop, installer).write_text("{")
    with pytest.raises(InstallError, match="not valid JSON"):
        installer.ensure_steamvr_settings(desktop, WANTED)


def install_steamvr(desktop, installer) -> None:
    launcher = desktop.home / installer.STEAMVR / "bin/linux64/vrcompositor-launcher"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")


@pytest.mark.parametrize("missing", ["steamvr", "plugin", "running"])
def test_vr_skips_without_touching_anything(installer, desktop, tmp_path, monkeypatch,
                                            capsys, missing: str) -> None:
    if missing != "steamvr":
        install_steamvr(desktop, installer)
    if missing != "plugin":
        make_plugin(desktop.home, installer)
    monkeypatch.setattr(installer, "steamvr_running", lambda: missing == "running")
    monkeypatch.setattr(installer, "fetch_release", lambda *_: pytest.fail("downloaded"))

    assert installer.vr(make_ctx(tmp_path), desktop) == frozenset()
    assert "vr stage skipped" in capsys.readouterr().out


@pytest.mark.parametrize(("handlers", "settings", "dropped"), [
    (True, True, set()),
    (False, True, {"vrmonitor_handler"}),
    (True, False, {"steamvr_settings"}),
])
def test_vr_owns_what_it_converged(installer, desktop, tmp_path, monkeypatch,
                                   handlers: bool, settings: bool, dropped: set[str]) -> None:
    install_steamvr(desktop, installer)
    make_plugin(desktop.home, installer)
    (tmp_path / "check.json").write_text(json.dumps(vr_manifest(desktop.home)))
    monkeypatch.setattr(installer, "steamvr_running", lambda: False)
    calls: list[str] = []
    for name in ("install_svlf", "install_ignition", "register_driver",
                 "install_psvr2toolkit", "install_unitysetup", "ensure_cap_sys_nice"):
        monkeypatch.setattr(installer, name, lambda *_a, name=name: calls.append(name))
    monkeypatch.setattr(installer, "ensure_scheme_handlers", lambda *_: handlers)
    monkeypatch.setattr(installer, "ensure_steamvr_settings", lambda *_: settings)

    owned = installer.vr(make_ctx(tmp_path), desktop)

    assert owned == installer.VR_CHECKS - dropped
    assert len(calls) == 6
    runtime = desktop.home / ".config/openxr/1/active_runtime.json"
    assert runtime.readlink() == desktop.home / installer.STEAMVR / "steamxr_linux64.json"


def test_vr_check_ids_are_solar_check_ids(installer) -> None:
    assert installer.VR_CHECKS <= set(CHECK_IDS)


# --- bluez stage ---------------------------------------------------------------------

TERRA = "terra-bluez-5.87+1.git8750129efca8-2.fc45.x86_64"
BLUEZ_ENV = {"BLUEZ_TERRA": TERRA, "BLUEZ_IMAGE": "fedora:45", "BLUEZ_INPUTS": "inputs-1"}


class BluezHost(Commands):
    """rpm, a podman build that writes ./bluetoothd, and records every other call."""

    def __init__(self, work: Path, terra: str = TERRA, build_ok: bool = True):
        super().__init__({("rpm", "-q", "terra-bluez"): (0, f"{terra}\n")})
        self.work, self.build_ok = work, build_ok

    def __call__(self, cmd, check=False, **kwargs):
        argv = list(cmd)
        if argv[:2] == ["podman", "run"]:
            self.seen.append(argv)
            if self.build_ok:
                (self.work / "bluetoothd").write_bytes(b"patched")
            return subprocess.CompletedProcess(argv, 0 if self.build_ok else 2,
                                               stdout="apply 0100\n", stderr="make: error\n")
        return super().__call__(cmd, check=check, **kwargs)


@pytest.fixture
def bluez_host(installer, tmp_path, monkeypatch):
    live = tmp_path / "live"
    monkeypatch.setattr(installer, "BLUETOOTHD", live / "bluetoothd")
    monkeypatch.setattr(installer, "BLUEZ_MARKER", live / "bluetoothd.built-for")
    monkeypatch.setattr(installer, "BLUEZ_STAMP", live / "bluetoothd.inputs")
    monkeypatch.setattr(installer, "BLUEZ_WORK", tmp_path / "cache" / "bluez-eagain")
    monkeypatch.setattr(installer, "steamvr_running", lambda: False)
    commands = BluezHost(installer.BLUEZ_WORK)
    for module in (installer, systemd):
        monkeypatch.setattr(module, "_run", commands)
    build = tmp_path / "build"
    (build / "bluez" / "patches").mkdir(parents=True)
    (build / "bluez" / "build.sh").write_text("build\n")
    (build / "bluez" / "patches" / "0100.patch").write_text("patch\n")
    (build / installer.BLUEZ_DROPIN).write_text("[Service]\n")
    dropin = tmp_path / "etc" / "50-eagain-patch.conf"
    ctx = InstallContext(host="solar", script_dir=tmp_path, build_dir=build, env=BLUEZ_ENV,
                         deploy_env={}, file_map={installer.BLUEZ_DROPIN: (str(dropin), "644")},
                         force_update=False)
    return ctx, commands


def converge_bluez(installer, ctx, inputs: str = "inputs-1") -> None:
    installer.BLUETOOTHD.parent.mkdir(parents=True, exist_ok=True)
    installer.BLUETOOTHD.write_bytes(b"patched")
    installer.BLUEZ_MARKER.write_text(f"{TERRA}\n")
    installer.BLUEZ_STAMP.write_text(f"{inputs}\n")
    dropin = Path(ctx.file_map[installer.BLUEZ_DROPIN][0])
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("[Service]\n")


def test_bluez_fresh_host_builds_installs_and_restarts(installer, bluez_host) -> None:
    ctx, commands = bluez_host

    assert installer.bluez(ctx) == installer.BLUEZ_CHECKS

    work = installer.BLUEZ_WORK
    podman = next(argv for argv in commands.seen if argv[:2] == ["podman", "run"])
    assert podman[-3:] == ["fedora:45", "bash", "/build/build.sh"]
    assert f"{work}:/build:Z" in podman and f"{work}/patches:/patches:ro,Z" in podman
    assert (work / "patches" / "0100.patch").is_file()
    assert (work / "inputs").read_text() == "inputs-1\n"
    assert installer.BLUETOOTHD.read_bytes() == b"patched"
    assert installer.BLUETOOTHD.stat().st_mode & 0o777 == 0o755
    assert installer.BLUEZ_MARKER.read_text() == f"{TERRA}\n"
    assert installer.BLUEZ_STAMP.read_text() == "inputs-1\n"
    assert Path(ctx.file_map[installer.BLUEZ_DROPIN][0]).read_text() == "[Service]\n"
    restart = first(commands, "systemctl", "restart", "bluetooth.service")
    assert first(commands, "semanage", "fcontext", "-a") < restart
    assert first(commands, "restorecon") < restart
    assert first(commands, "systemctl", "daemon-reload") < restart


def test_bluez_converged_host_is_left_alone(installer, bluez_host) -> None:
    ctx, commands = bluez_host
    converge_bluez(installer, ctx)

    assert installer.bluez(ctx) == installer.BLUEZ_CHECKS

    assert [argv[0] for argv in commands.seen] == ["rpm"]


def test_bluez_dropin_drift_restarts_without_a_build(installer, bluez_host) -> None:
    ctx, commands = bluez_host
    converge_bluez(installer, ctx)
    Path(ctx.file_map[installer.BLUEZ_DROPIN][0]).write_text("edited\n")

    assert installer.bluez(ctx) == installer.BLUEZ_CHECKS

    assert not any(argv[:1] in (["podman"], ["semanage"]) for argv in commands.seen)
    assert ["systemctl", "restart", "bluetooth.service"] in commands.seen


def test_bluez_reuses_a_cached_build_of_the_same_inputs(installer, bluez_host) -> None:
    ctx, commands = bluez_host
    converge_bluez(installer, ctx, inputs="inputs-0")
    installer.BLUEZ_WORK.mkdir(parents=True)
    (installer.BLUEZ_WORK / "bluetoothd").write_bytes(b"cached")
    (installer.BLUEZ_WORK / "inputs").write_text("inputs-1\n")

    installer.bluez(ctx)

    assert not any(argv[:1] == ["podman"] for argv in commands.seen)
    assert installer.BLUETOOTHD.read_bytes() == b"cached"
    assert installer.BLUEZ_STAMP.read_text() == "inputs-1\n"


def test_bluez_refuses_another_terra_bluez(installer, bluez_host, monkeypatch) -> None:
    ctx, _ = bluez_host
    commands = BluezHost(installer.BLUEZ_WORK, terra="terra-bluez-5.88-1.fc45.x86_64")
    monkeypatch.setattr(installer, "_run", commands)

    with pytest.raises(InstallError, match="has terra-bluez-5.88-1.fc45.x86_64, bluez-eagain "
                                           "is written for terra-bluez-5.87"):
        installer.bluez(ctx)
    assert [argv[0] for argv in commands.seen] == ["rpm"]
    assert not installer.BLUETOOTHD.exists()


def test_bluez_refuses_an_image_without_terra_bluez(installer, bluez_host, monkeypatch) -> None:
    ctx, _ = bluez_host
    monkeypatch.setattr(installer, "_run", Commands(
        {("rpm",): (1, "package terra-bluez is not installed\n")}))

    with pytest.raises(InstallError, match="the image has no terra-bluez"):
        installer.bluez(ctx)


@pytest.mark.parametrize("when", ["before", "during"])
def test_bluez_never_restarts_under_steamvr(installer, bluez_host, monkeypatch, capsys,
                                            when: str) -> None:
    ctx, commands = bluez_host
    answers = iter([when == "before", True])
    monkeypatch.setattr(installer, "steamvr_running", lambda: next(answers))

    assert installer.bluez(ctx) == frozenset()

    built = any(argv[:1] == ["podman"] for argv in commands.seen)
    assert built is (when == "during")
    assert not installer.BLUETOOTHD.exists()
    assert not any(argv[:2] == ["systemctl", "restart"] for argv in commands.seen)
    assert "SteamVR" in capsys.readouterr().out


def test_bluez_failed_build_installs_nothing(installer, bluez_host, monkeypatch) -> None:
    ctx, _ = bluez_host
    commands = BluezHost(installer.BLUEZ_WORK, build_ok=False)
    for module in (installer, systemd):
        monkeypatch.setattr(module, "_run", commands)

    with pytest.raises(InstallError, match=r"bluetoothd build failed \(logs in .*\):\n"
                                           r"apply 0100\nmake: error"):
        installer.bluez(ctx)
    assert not installer.BLUETOOTHD.exists()
    assert not (installer.BLUEZ_WORK / "inputs").exists()


def test_bluez_semanage_modifies_an_existing_rule(installer, bluez_host, monkeypatch) -> None:
    ctx, commands = bluez_host
    commands.outputs[("semanage", "fcontext", "-a")] = (1, "")

    installer.bluez(ctx)

    assert ["semanage", "fcontext", "-m", "-t", "bluetooth_exec_t",
            str(installer.BLUETOOTHD)] in commands.seen


def test_bluez_check_ids_are_solar_check_ids(installer) -> None:
    assert installer.BLUEZ_CHECKS <= set(CHECK_IDS)


def test_bluez_terra_pin_matches_build_sh() -> None:
    commit = vr_guest.BLUEZ_TERRA.split(".git", 1)[1].split("-", 1)[0]
    build_sh = (ROOT / "vr-guest" / "bluez-eagain" / "build.sh").read_text(encoding="utf-8")
    assert f"bluez/archive/{commit}.tar.gz" in build_sh


def test_bluez_inputs_follow_every_build_input(tmp_path: Path) -> None:
    root = fake_root(tmp_path)
    before = vr_guest.bluez_inputs(root)
    patch = vr_guest.bluez_patches(root)[-1]
    patch.write_text(patch.read_text() + "\n")
    after_patch = vr_guest.bluez_inputs(root)
    build_sh = root / "vr-guest" / "bluez-eagain" / "build.sh"
    build_sh.write_text(build_sh.read_text() + "\n")

    assert len({before, after_patch, vr_guest.bluez_inputs(root)}) == 3


def test_validate_needs_bluez_patches(tmp_path: Path) -> None:
    root = fake_root(tmp_path)
    for patch in vr_guest.bluez_patches(root):
        patch.unlink()

    with pytest.raises(ValueError, match="no patches in"):
        vr_guest.validate(root, ["solar"])


# --- goxlr stage -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "version"),
    [("goxlr-daemon 1.2.4\n", "1.2.4"), ("", ""), ("bash: goxlr-daemon: not found\n", "")],
)
def test_goxlr_version(installer, stdout: str, version: str) -> None:
    installer._run = Commands({(f"{installer.LOCAL_BIN}/goxlr-daemon",): (0, stdout)})
    assert installer.goxlr_version() == version


def test_goxlr_version_without_the_binary(installer, monkeypatch) -> None:
    def missing(*_a, **_k):
        raise FileNotFoundError

    installer._run = missing
    assert installer.goxlr_version() == ""


class RpmHost(Commands):
    """`cpio` unpacks the named members into its cwd, as the real one would."""

    def __init__(self, members: tuple[str, ...], version: str = ""):
        super().__init__({("rpm2cpio",): (0, b"payload")})
        self.members, self.version = members, version
        self.payload = None

    def __call__(self, cmd, check=False, **kwargs):
        argv = list(cmd)
        if argv[0].endswith("/goxlr-daemon"):
            self.seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=self.version, stderr="")
        if argv[0] == "cpio":
            self.payload = kwargs["input"]
            for name in self.members:
                path = Path(kwargs["cwd"], "usr/bin", name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{name} new")
        return super().__call__(cmd, check, **kwargs)


def goxlr_ctx(tmp_path: Path) -> InstallContext:
    return make_ctx(tmp_path, {"GOXLR_VERSION": "1.2.4", "GOXLR_URL": "https://h/g.rpm",
                               "GOXLR_SHA": "x"})


def test_goxlr_installs_every_binary_from_the_pinned_rpm(installer, tmp_path,
                                                         monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "goxlr-daemon").write_text("old")
    installer.LOCAL_BIN = str(bin_dir)
    host = RpmHost(installer.GOXLR_BINARIES, version="goxlr-daemon 1.2.3\n")
    installer._run = host
    fetched = []
    monkeypatch.setattr(installer, "fetch",
                        lambda url, digest: fetched.append((url, digest)) or tmp_path / "g.rpm")

    installer.ensure_goxlr_binaries(goxlr_ctx(tmp_path))

    assert fetched == [("https://h/g.rpm", "x")]
    assert host.payload == b"payload"
    for name in ("goxlr-daemon", "goxlr-client", "goxlr-launcher", "goxlr-defaults"):
        assert (bin_dir / name).read_text() == f"{name} new"
        assert (bin_dir / name).stat().st_mode & 0o777 == 0o755
        assert ["restorecon", str(bin_dir / name)] in host.seen
    assert not list(bin_dir.glob(".*"))


def test_goxlr_rpm_without_a_binary_is_refused(installer, tmp_path, monkeypatch) -> None:
    installer.LOCAL_BIN = str(tmp_path)
    installer._run = RpmHost(("goxlr-daemon", "goxlr-client"))
    monkeypatch.setattr(installer, "fetch", lambda url, digest: tmp_path / "g.rpm")

    with pytest.raises(InstallError, match="g.rpm has no /usr/bin/goxlr-launcher"):
        installer.ensure_goxlr_binaries(goxlr_ctx(tmp_path))


def test_the_pinned_goxlr_is_left_alone(installer, tmp_path, monkeypatch) -> None:
    installer.LOCAL_BIN = str(tmp_path)
    host = RpmHost(installer.GOXLR_BINARIES, version="goxlr-daemon 1.2.4\n")
    installer._run = host
    monkeypatch.setattr(installer, "fetch", lambda *_a: pytest.fail("fetched"))

    installer.ensure_goxlr_binaries(goxlr_ctx(tmp_path))

    assert host.seen == [[f"{tmp_path}/goxlr-daemon", "--version"]]


def test_goxlr_daemon_already_running_is_never_restarted(installer) -> None:
    commands = Commands({("pgrep",): (0, "")})
    installer._run = commands

    installer.ensure_goxlr_daemon("freender")

    assert commands.seen == [["pgrep", "-u", "freender", "-x", "goxlr-daemon"]]


def test_goxlr_daemon_waits_for_a_login(installer, monkeypatch, capsys) -> None:
    installer._run = Commands({("pgrep",): (1, "")})
    monkeypatch.setattr(installer, "session_env", lambda uid: None)

    installer.ensure_goxlr_daemon(os.environ.get("USER") or "root")

    assert "goxlr-daemon starts at login" in capsys.readouterr().out


def test_goxlr_daemon_starts_in_the_session(installer, monkeypatch) -> None:
    commands = Commands({("pgrep",): (1, "")})
    installer._run = commands
    session = {"XDG_RUNTIME_DIR": "/run/user/1000",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    monkeypatch.setattr(installer, "session_env", lambda uid: session)
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda user: type("A", (), {"pw_uid": 1000}))

    installer.ensure_goxlr_daemon("freender")

    assert commands.seen[1] == [
        "runuser", "-u", "freender", "--", "env", "XDG_RUNTIME_DIR=/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        "systemd-run", "--user", "--collect", "--unit=goxlr-daemon", "/usr/local/bin/goxlr-daemon",
    ]


@pytest.mark.parametrize(("changed", "triggered"), [
    ({"etc/udev/rules.d/50-goxlr.rules"}, True),
    ({"etc/udev/rules.d/71-psvr2-steamvr.rules"}, False),
    (set(), False),
])
def test_udev_triggers_the_goxlr_only_when_its_rule_changed(installer, tmp_path, monkeypatch,
                                                            changed, triggered) -> None:
    commands = Commands()
    installer._run = commands
    monkeypatch.setattr(installer.files, "install_validated", lambda *_a: False)
    monkeypatch.setattr(installer.files, "install", lambda ctx, name: name in changed)
    monkeypatch.setattr(installer.systemd, "ensure_running", lambda *_a: None)

    installer.install_etc(make_ctx(tmp_path))

    assert (["udevadm", "control", "--reload"] in commands.seen) is bool(changed)
    assert (["udevadm", "trigger", "--subsystem-match=usb", "--attr-match=idVendor=1220"]
            in commands.seen) is triggered


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


DB_DIFF = """\
ostree diff commit from: booted deployment (8b7f)
ostree diff commit to:   pending deployment (9070)
Upgraded:
  kernel-headers 7.3.3-300.fc45 -> 7.3.4-300.fc45
  kmod-nvidia 3:620.70.06-1.fc45 -> 3:620.70.06-2.fc45
Removed:
  terra-bluez-5.87+1.git8750129efca8-2.fc45.x86_64
  kernel-srpm-macros-1.0-30.fc45.noarch
"""


def test_pending_changes_match_exact_package_names(monkeypatch) -> None:
    metrics = load_script("solar_metrics", METRICS_PATH)
    monkeypatch.setattr(metrics, "run", lambda *cmd: subprocess.CompletedProcess(cmd, 0, DB_DIFF))

    lines = metrics.pending_change_metrics()

    # kernel-headers is not the booted kernel; a removed terra-bluez is a bluez change.
    assert [line for line in lines if not line.startswith("#")] == [
        'homelab_ostree_pending_package_changed{package="bluez"} 1',
        'homelab_ostree_pending_package_changed{package="kernel"} 0',
        'homelab_ostree_pending_package_changed{package="kmod-nvidia"} 1',
    ]


def test_a_failed_db_diff_publishes_no_change_series(monkeypatch) -> None:
    metrics = load_script("solar_metrics", METRICS_PATH)
    monkeypatch.setattr(metrics, "run", lambda *cmd: subprocess.CompletedProcess(cmd, 1, ""))

    assert metrics.pending_change_metrics() == []


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


WANTED = {"showMirrorView": False, "supersampleScale": 1.18}


@pytest.mark.parametrize(
    ("settings", "passes"),
    [
        # SteamVR writes 1.18 back as 1.1799999999999999: the same double.
        ('{"steamvr": {"showMirrorView": false, "supersampleScale": 1.1799999999999999,'
         ' "other": 1}}', True),
        ('{"steamvr": {"showMirrorView": false}}', False),
        ('{"steamvr": {"showMirrorView": true, "supersampleScale": 1.18}}', False),
        ("[]", False),
        ("{", False),
    ],
)
def test_steamvr_settings(check, tmp_path, settings: str, passes: bool) -> None:
    path = tmp_path / "steamvr.vrsettings"
    path.write_text(settings)
    assert check.steamvr_settings_ok(path, WANTED) is passes
    assert check.steamvr_settings_ok(tmp_path / "missing", WANTED) is False


def vr_manifest(home: Path) -> dict:
    return {"home": str(home), "psvr2toolkit_dll_sha256": hashlib.sha256(b"toolkit").hexdigest(),
            "steamvr_settings": WANTED}


@pytest.mark.parametrize(("dll", "passes"), [(b"toolkit", True), (b"sony", False), (None, False)])
def test_psvr2toolkit_dll_compares_the_pinned_hash(check, tmp_path, monkeypatch, capsys,
                                                   dll: bytes | None, passes: bool) -> None:
    win64 = tmp_path / check.PLUGIN / "bin/win64"
    win64.mkdir(parents=True)
    if dll is not None:
        (win64 / "driver_playstation_vr2.dll").write_bytes(dll)
    monkeypatch.setattr(check, "output", lambda *cmd: "")

    check.check_vr(check.Report(), vr_manifest(tmp_path))

    line = next(line for line in capsys.readouterr().out.splitlines() if "psvr2toolkit" in line)
    assert line.startswith("  ok  " if passes else "  FAIL")


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

    check.check_bluez(report, {"bluez_inputs": "x"})

    line = capsys.readouterr().out.splitlines()[1]
    status = "ok  " if passes else "FAIL"
    assert line.startswith(f"  {status}  bluez_build_matches")


@pytest.mark.parametrize(("stamp", "passes"), [("abc", True), ("old", False), (None, False)])
def test_bluez_build_inputs(check, tmp_path, monkeypatch, capsys, stamp, passes) -> None:
    check.BLUEZ_STAMP = tmp_path / "inputs"
    if stamp:
        check.BLUEZ_STAMP.write_text(stamp + "\n")
    monkeypatch.setattr(check, "output", lambda *cmd: "")

    check.check_bluez(check.Report(), {"bluez_inputs": "abc"})

    line = capsys.readouterr().out.splitlines()[2]
    assert line.startswith(f"  {'ok  ' if passes else 'FAIL'}  bluez_build_inputs")


@pytest.mark.parametrize(
    ("doctor", "passed", "reason"),
    [('{"outputs": [{"name": "HDMI-A-1", "connected": true, "ddcCiAllowed": false},'
      ' {"name": "DP-1", "connected": false, "ddcCiAllowed": true}]}', True, ""),
     ('{"outputs": [{"name": "HDMI-A-1", "connected": true, "ddcCiAllowed": true}]}', False,
      "DDC/CI allowed on HDMI-A-1"),
     ("", False, "kscreen-doctor -j unreadable")],
)
def test_ddc_ci_off(check, tmp_path, monkeypatch, doctor, passed, reason) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(check, "output", lambda *cmd: seen.append(cmd) or doctor)
    (tmp_path / "wayland-0.lock").touch()
    (tmp_path / "wayland-0").touch()

    ok, why = check.ddc_ci_off(tmp_path)

    assert ok is passed
    assert passed or why == reason
    assert seen == [("env", f"XDG_RUNTIME_DIR={tmp_path}", "WAYLAND_DISPLAY=wayland-0",
                     "kscreen-doctor", "-j")]


def test_ddc_ci_off_needs_a_wayland_session(check, tmp_path) -> None:
    assert check.ddc_ci_off(tmp_path) == (False, "no Wayland session")


def test_check_unreadable_manifest(check, tmp_path) -> None:
    assert check.main(tmp_path / "missing.json") == 2


@pytest.mark.parametrize(
    ("stdout", "active", "failed"),
    [("vector 0.61.0 (x86_64-unknown-linux-gnu)", True, []),
     ("vector 0.60.1 (x86_64-unknown-linux-gnu)", True, ["vector_version"]),
     ("", False, ["vector_version", "vector_active"])],
)
def test_check_vector(check, monkeypatch, capsys, stdout: str, active: bool,
                      failed: list[str]) -> None:
    monkeypatch.setattr(check, "output", lambda *cmd: stdout)
    monkeypatch.setattr(check, "succeeds", lambda *cmd: active)

    check.check_vector(check.Report(), {"vector_version": "0.61.0"})

    out = capsys.readouterr().out
    assert [line.split()[1].rstrip(":") for line in out.splitlines()
            if line.startswith("  FAIL")] == failed


@pytest.mark.parametrize(
    ("stdout", "failed"),
    [("goxlr-daemon 1.2.4", []), ("goxlr-daemon 1.2.3", ["goxlr_version"]),
     ("", ["goxlr_version"])],
)
def test_check_goxlr(check, monkeypatch, capsys, stdout: str, failed: list[str]) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(check, "output", lambda *cmd: seen.append(cmd) or stdout)

    check.check_goxlr(check.Report(), {"goxlr_version": "1.2.4"})

    assert seen == [("/usr/local/bin/goxlr-daemon", "--version")]
    out = capsys.readouterr().out
    assert [line.split()[1].rstrip(":") for line in out.splitlines()
            if line.startswith("  FAIL")] == failed
