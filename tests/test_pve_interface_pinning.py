"""pve-interface-pinning: orchestrator file map and remote installer (freender/homelab-ops#30).

Every destination and every scanned directory is rebound under `tmp_path`, and
`systemctl`/`udevadm` are faked. Nothing here touches `/etc/systemd/network` or a
real interface. The golden render of the `.link` contents lives in
`test_render_golden.py`.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import pve_interface_pinning as pinning
from homelab_install import systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.main import _parse_file_map

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "pve-interface-pinning" / "scripts" / "install.py"
SERVICE = pinning.WOL_SERVICE

NIC0 = pinning.InterfacePin(
    name="nic0", mac="38:ea:a7:91:cc:60", role="management", wake_on_lan=False
)
NIC2 = pinning.InterfacePin(name="nic2", mac="e0:d5:5e:25:a5:e0", role="wol", wake_on_lan=True)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class FakeRegistry:
    def __init__(self, interfaces: list[dict]) -> None:
        self.interfaces = interfaces

    def get(self, host: str, key: str, default=None):
        assert key == "pve-interface-pinning.interfaces"
        return self.interfaces


def render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interfaces: list[dict]):
    monkeypatch.setattr(pinning, "default_registry", lambda _root: FakeRegistry(interfaces))
    return pinning.build_host_artifacts(tmp_path, "ace")


ACE = [
    {"name": "nic0", "role": "management", "mac": NIC0.mac},
    {"name": "nic2", "role": "wol", "mac": NIC2.mac, "wake_on_lan": True},
]


def test_file_map_matches_what_was_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map is the only list the installer reads; a rendered file outside it
    would never be installed, and a map entry without a file raises on the host."""
    artifacts = render(tmp_path, monkeypatch, ACE)

    file_map = _parse_file_map(artifacts.build_dir / "file-map.conf")
    rendered = {p.name for p in artifacts.build_dir.iterdir()} - {"file-map.conf"}
    assert set(file_map) == rendered == {spec.build_name for spec in artifacts.file_specs}
    assert "link-files.conf" not in rendered


def test_link_files_land_in_etc_systemd_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = render(tmp_path, monkeypatch, ACE)
    file_map = _parse_file_map(artifacts.build_dir / "file-map.conf")

    links = {name: dest for name, (dest, _mode) in file_map.items() if name.endswith(".link")}
    assert links == {
        "10-homelab-nic0.link": "/etc/systemd/network/10-homelab-nic0.link",
        "10-homelab-nic2.link": "/etc/systemd/network/10-homelab-nic2.link",
    }
    assert file_map["homelab-interface-wol"] == ("/usr/local/sbin/homelab-interface-wol", "755")
    assert file_map[SERVICE] == (f"/etc/systemd/system/{SERVICE}", "644")


@pytest.mark.parametrize(("wol", "expected"), [(True, f"nic2|{NIC2.mac}\n"), (False, "")])
def test_the_wol_config_lists_only_wol_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wol: bool, expected: str
) -> None:
    interfaces = [dict(ACE[0]), {**ACE[1], "wake_on_lan": wol}]
    artifacts = render(tmp_path, monkeypatch, interfaces)

    assert (artifacts.build_dir / pinning.WOL_CONFIG).read_text(encoding="utf-8") == expected


def test_the_wol_service_runs_the_script_the_map_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = render(tmp_path, monkeypatch, ACE)
    file_map = _parse_file_map(artifacts.build_dir / "file-map.conf")

    service = (artifacts.build_dir / SERVICE).read_text(encoding="utf-8")
    assert f"ExecStart={file_map['homelab-interface-wol'][0]}\n" in service


def test_validate_requires_the_python_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pinning, "default_registry", lambda _root: FakeRegistry(ACE))
    with pytest.raises(ValueError, match="install.py"):
        pinning.validate(tmp_path, [])


def test_orchestrator_stages_the_python_installer() -> None:
    assert pinning.INSTALLER == "scripts/install.py"
    assert pinning.INTERPRETER == "python3"
    assert (REPO_ROOT / "pve-interface-pinning" / pinning.INSTALLER).is_file()
    assert not (REPO_ROOT / "pve-interface-pinning" / "scripts" / "install.sh").exists()


def test_file_names_agree_across_the_wire() -> None:
    installer = load_installer()
    assert installer.WOL_SERVICE == pinning.WOL_SERVICE
    assert installer.WOL_CONFIG == pinning.WOL_CONFIG


# ---------------------------------------------------------------------------
# Remote installer
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("pinning_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSystemctl:
    def __init__(self) -> None:
        self.enabled = {SERVICE}
        self.active = {SERVICE}
        self.calls: list[list[str]] = []
        self.udev_code = 0

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        code = 0
        if command[0] == "udevadm":
            code = self.udev_code
        else:
            verb, unit = command[1], command[-1]
            if verb == "is-enabled":
                code = 0 if unit in self.enabled else 1
            elif verb == "is-active":
                code = 0 if unit in self.active else 3
            elif verb == "enable":
                self.enabled.add(unit)
                self.active.add(unit)
            elif verb == "disable":
                self.enabled.discard(unit)
                self.active.discard(unit)
        return subprocess.CompletedProcess(command, code, "", "")

    def actions(self) -> list[str]:
        queries = {"is-enabled", "is-active"}
        return [
            " ".join(call[1:]) if call[0] == "systemctl" else " ".join(call)
            for call in self.calls
            if call[1] not in queries
        ]


class Host:
    """A sandboxed host filesystem plus a staged bundle rendered by the orchestrator."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pins) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "hostfs"
        self.systemctl = FakeSystemctl()
        monkeypatch.setattr(systemd, "_run", self.systemctl)
        monkeypatch.setattr(self.installer, "_run", self.systemctl)

        self.etc_network = self.root / "etc" / "systemd" / "network"
        self.vendor = self.root / "usr" / "lib" / "systemd" / "network"
        self.vendor.mkdir(parents=True)
        (self.root / "lib").symlink_to(self.root / "usr" / "lib")
        monkeypatch.setattr(
            self.installer,
            "NETWORK_DIRS",
            (
                str(self.etc_network),
                str(self.root / "run" / "systemd" / "network"),
                str(self.vendor),
                str(self.root / "lib" / "systemd" / "network"),
            ),
        )
        self.udev_rule = self.root / "etc" / "udev" / "rules.d" / "70-persistent-net.rules"
        monkeypatch.setattr(self.installer, "LEGACY_UDEV_RULE", str(self.udev_rule))

        self.build_dir = tmp_path / "remote" / "build" / "h"
        self.force = False
        self.set_pins(pins)

    def set_pins(self, pins) -> None:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.build_dir.iterdir():
            stale.unlink()
        specs = []
        for pin in pins:
            name = f"10-homelab-{pin.name}.link"
            (self.build_dir / name).write_text(pinning.link_file(pin, "h"), encoding="utf-8")
            specs.append(pinning.FileSpec(name, f"/etc/systemd/network/{name}"))
        (self.build_dir / pinning.WOL_CONFIG).write_text(
            "".join(f"{pin.name}|{pin.mac}\n" for pin in pins if pin.wake_on_lan), encoding="utf-8"
        )
        (self.build_dir / "homelab-interface-wol").write_text(
            pinning.wol_script(), encoding="utf-8"
        )
        (self.build_dir / SERVICE).write_text(pinning.wol_service(), encoding="utf-8")
        specs += [
            pinning.FileSpec(pinning.WOL_CONFIG, f"/etc/homelab/{pinning.WOL_CONFIG}", "644"),
            pinning.FileSpec(
                "homelab-interface-wol", "/usr/local/sbin/homelab-interface-wol", "755"
            ),
            pinning.FileSpec(SERVICE, f"/etc/systemd/system/{SERVICE}", "644"),
        ]
        self.file_map = {
            spec.build_name: (str(self.root / spec.remote_path.lstrip("/")), spec.mode)
            for spec in specs
        }

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def deploy(self) -> None:
        self.systemctl.calls.clear()
        self.installer.install(
            InstallContext(
                host="h",
                script_dir=self.build_dir.parents[1],
                build_dir=self.build_dir,
                env={},
                deploy_env={},
                file_map=self.file_map,
                force_update=self.force,
            )
        )


@pytest.fixture
def host_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return lambda *pins: Host(tmp_path, monkeypatch, pins or (NIC0, NIC2))


def test_a_fresh_host_gets_every_file_reloads_udev_and_warns_about_reboot(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.systemctl.enabled = set()
    host.systemctl.active = set()
    host.deploy()

    assert host.dest("10-homelab-nic0.link").read_text(encoding="utf-8") == pinning.link_file(
        NIC0, "h"
    )
    assert host.dest("homelab-interface-wol").stat().st_mode & 0o777 == 0o755
    assert host.systemctl.actions() == [
        "udevadm control --reload",
        "daemon-reload",
        f"enable --now {SERVICE}",
    ]
    assert "Pinned interface name(s) changed" in capsys.readouterr().out


def test_an_unchanged_redeploy_touches_nothing_and_needs_no_reboot(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.deploy()
    capsys.readouterr()
    host.deploy()

    assert host.systemctl.actions() == []
    out = capsys.readouterr().out
    assert "no reboot needed" in out
    assert "Warning" not in out


def test_a_forced_redeploy_does_not_claim_names_changed(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--force` rewrites every file; the reboot warning must still read content."""
    host = host_for()
    host.deploy()
    capsys.readouterr()
    host.force = True
    host.deploy()

    out = capsys.readouterr().out
    assert "Pinned interface name(s) changed" not in out
    assert "no reboot needed" in out


def test_a_renamed_pin_reloads_udev_warns_and_leaves_wol_alone(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for(NIC0)
    host.deploy()
    capsys.readouterr()
    host.set_pins([pinning.InterfacePin("nic0", "11:22:33:44:55:66", "management", False)])
    host.deploy()

    assert host.systemctl.actions() == ["udevadm control --reload"]
    assert "Pinned interface name(s) changed" in capsys.readouterr().out


def test_a_failed_udev_reload_warns_but_does_not_fail(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.systemctl.udev_code = 1
    host.deploy()

    assert "failed to reload udev rules" in capsys.readouterr().out


def test_a_new_wol_interface_reruns_the_oneshot(host_for) -> None:
    """`enable --now` on an active RemainAfterExit unit does nothing, so the bash
    left a newly added WOL NIC unarmed until the next boot."""
    host = host_for(NIC0, NIC2)
    host.deploy()
    host.set_pins([NIC0, NIC2, pinning.InterfacePin("nic3", "58:47:ca:7f:77:2a", "wol", True)])
    host.deploy()

    assert host.systemctl.actions() == [
        "udevadm control --reload",
        "daemon-reload",
        f"restart {SERVICE}",
    ]


def test_no_wol_pins_stops_the_service_and_keeps_its_files(host_for) -> None:
    host = host_for(NIC0)
    host.deploy()

    assert host.systemctl.actions()[-1] == f"disable --now {SERVICE}"
    assert host.dest(SERVICE).exists()
    assert host.dest(pinning.WOL_CONFIG).read_text(encoding="utf-8") == ""


def test_a_wol_host_losing_its_last_wol_pin_reloads_before_stopping(host_for) -> None:
    host = host_for(NIC0, NIC2)
    host.deploy()
    host.set_pins([NIC0, pinning.InterfacePin("nic2", NIC2.mac, "wol", False)])
    host.deploy()

    assert host.systemctl.actions() == [
        "udevadm control --reload",
        "daemon-reload",
        f"disable --now {SERVICE}",
    ]


def test_an_empty_pin_set_fails_before_anything_is_touched(host_for) -> None:
    host = host_for()
    host.file_map = {name: v for name, v in host.file_map.items() if not name.endswith(".link")}

    with pytest.raises(InstallError, match="no .link files"):
        host.deploy()
    assert host.systemctl.calls == []
    assert not host.etc_network.exists()


# --- competing-rule scan ---------------------------------------------------------


def warnings(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Scan warnings only -- a fresh host also gets the reboot warning, correctly."""
    lines = capsys.readouterr().out.splitlines()
    return [line.strip() for line in lines if "Warning" in line and "Pinned interface" not in line]


def test_vendor_defaults_raise_no_warning(host_for, capsys: pytest.CaptureFixture[str]) -> None:
    """The real `/usr/lib/systemd/network` contents on every node."""
    host = host_for()
    (host.vendor / "99-default.link").write_text(
        "[Match]\nOriginalName=*\n\n[Link]\nNamePolicy=keep kernel database onboard slot path\n"
        "AlternativeNamesPolicy=database onboard slot path mac\nMACAddressPolicy=persistent\n",
        encoding="utf-8",
    )
    (host.vendor / "73-usb-net-by-mac.link").write_text(
        "[Match]\nPath=*-usb-*\nProperty=ID_NET_NAME_MAC=*\n\n[Link]\nNamePolicy=mac\n",
        encoding="utf-8",
    )
    host.deploy()
    host.deploy()

    assert warnings(capsys) == []


def test_a_foreign_file_claiming_a_pinned_mac_warns_once_despite_the_lib_symlink(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    (host.vendor / "50-vendor.link").write_text(
        f"[Match]\nMACAddress=aa:bb:cc:dd:ee:ff {NIC2.mac.upper()}\n[Link]\nName=eth9\n",
        encoding="utf-8",
    )
    host.deploy()

    found = warnings(capsys)
    assert len(found) == 1
    assert f"matches pinned MAC {NIC2.mac} (target name nic2)" in found[0]


def test_a_foreign_file_taking_a_pinned_name_warns(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.etc_network.mkdir(parents=True)
    (host.etc_network / "05-other.link").write_text(
        "[Match]\nDriver=igb\n[Link]\nName=nic0\n", encoding="utf-8"
    )
    host.deploy()

    assert any("also targets name nic0" in line for line in warnings(capsys))


def test_original_and_alternative_names_are_not_assignments(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bash's `grep "Name=nic0$"` matched both of these."""
    host = host_for()
    (host.vendor / "60-alt.link").write_text(
        "[Match]\nOriginalName=nic0\n[Link]\nAlternativeName=nic2\n", encoding="utf-8"
    )
    host.deploy()

    assert warnings(capsys) == []


def test_a_same_named_file_elsewhere_is_shadowed_not_foreign(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    (host.vendor / "10-homelab-nic0.link").write_text(
        f"[Match]\nMACAddress={NIC0.mac}\n", encoding="utf-8"
    )
    host.deploy()

    assert warnings(capsys) == []


def test_a_stale_homelab_link_from_an_old_pin_is_reported(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not deleted -- a pin rename leaves the old file claiming the same MAC, and
    lexical order may let it win. Warning is the whole response."""
    host = host_for()
    host.etc_network.mkdir(parents=True)
    old = host.etc_network / "10-homelab-lan.link"
    old.write_text(
        pinning.link_file(pinning.InterfacePin("lan", NIC0.mac, "management", False), "h"),
        encoding="utf-8",
    )
    host.deploy()

    assert any(str(old) in line and NIC0.mac in line for line in warnings(capsys))
    assert old.exists()


def test_the_legacy_udev_rule_warns(host_for, capsys: pytest.CaptureFixture[str]) -> None:
    host = host_for()
    host.udev_rule.parent.mkdir(parents=True)
    host.udev_rule.write_text("", encoding="utf-8")
    host.deploy()

    assert any("70-persistent-net.rules" in line for line in warnings(capsys))


def test_an_unreadable_foreign_file_is_skipped(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    (host.vendor / "broken.link").symlink_to(host.root / "nowhere")
    host.deploy()

    assert warnings(capsys) == []


def test_a_map_entry_without_its_rendered_link_fails_cleanly(host_for) -> None:
    host = host_for()
    (host.build_dir / "10-homelab-nic2.link").unlink()

    with pytest.raises(InstallError, match="10-homelab-nic2.link"):
        host.deploy()
    assert host.systemctl.calls == []
    assert not host.etc_network.exists()
