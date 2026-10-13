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
from homelab_install.main import _parse_env_file, _parse_file_map

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
        if key == "pve-interface-pinning.interfaces":
            return self.interfaces
        return default


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
    rendered = {p.name for p in artifacts.build_dir.iterdir()} - {"file-map.conf", "env"}
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


class VmRegistry:
    """A `kind: qemu` guest: its pins come from `pve-guest.net`."""

    def __init__(self, interfaces: list[dict] | None = None) -> None:
        self.interfaces = interfaces

    def get(self, host: str, key: str, default=None):
        values = {
            "config.type": "ubuntu",
            "pve-guest.kind": "qemu",
            "pve-guest.net": {
                "net0": {"name": "nic0", "mac": "BC:24:11:18:E3:17", "tag": 40},
                "net1": {"name": "nic1", "mac": "BC:24:11:00:00:01"},
            },
            "pve-interface-pinning.interfaces": self.interfaces,
        }
        value = values.get(key)
        return default if value is None else value

    def has(self, host: str, feature: str) -> bool:
        return False


def render_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pinning, "default_registry", lambda _root: VmRegistry())
    return pinning.build_host_artifacts(tmp_path, "helm")


def test_a_vm_takes_its_pins_from_pve_guest_net() -> None:
    assert pinning.normalize_interface_pins(VmRegistry(), "helm") == (
        pinning.InterfacePin("nic0", "bc:24:11:18:e3:17", "management", False),
        pinning.InterfacePin("nic1", "bc:24:11:00:00:01", "net1", False),
    )


def test_a_vm_may_not_declare_its_nics_twice() -> None:
    registry = VmRegistry(interfaces=[{"name": "nic0", "mac": "bc:24:11:18:e3:17"}])
    with pytest.raises(ValueError, match="declare its NICs in pve-guest.net"):
        pinning.normalize_interface_pins(registry, "helm")


def test_a_vm_gets_a_network_file_next_to_each_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = render_vm(tmp_path, monkeypatch)
    file_map = _parse_file_map(artifacts.build_dir / "file-map.conf")

    networks = {name: dest for name, (dest, _mode) in file_map.items() if name.endswith(".network")}
    assert networks == {
        "10-homelab-nic0.network": "/etc/systemd/network/10-homelab-nic0.network",
        "10-homelab-nic1.network": "/etc/systemd/network/10-homelab-nic1.network",
    }
    rendered = (artifacts.build_dir / "10-homelab-nic1.network").read_text(encoding="utf-8")
    assert "[Match]\nName=nic1\n" in rendered
    assert "[Network]\nDHCP=ipv4\n" in rendered
    assert "[DHCPv4]\nClientIdentifier=mac\n" in rendered


def test_a_vm_env_turns_on_the_decloud_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = render_vm(tmp_path, monkeypatch)
    env = _parse_env_file(artifacts.build_dir / "env")

    assert env["VM_GUEST"] == "true"
    assert env["CLOUD_PACKAGES"].split() == list(pinning.CLOUD_PACKAGES)
    assert "cloud-init" in env["CLOUD_PACKAGES"].split()
    assert env["CLOUD_PATHS"].split() == list(pinning.CLOUD_PATHS)
    assert env["RETIRED_NETWORK_FILES"] == "/etc/systemd/network/10-lan.network"


def test_a_pve_node_gets_no_network_files_and_no_vm_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = render(tmp_path, monkeypatch, ACE)
    env = _parse_env_file(artifacts.build_dir / "env")

    assert not list(artifacts.build_dir.glob("*.network"))
    assert env == {
        "VM_GUEST": "false",
        "CLOUD_PACKAGES": "",
        "CLOUD_PATHS": "",
        "RETIRED_NETWORK_FILES": "",
    }


def test_validate_requires_the_python_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pinning, "default_registry", lambda _root: FakeRegistry(ACE))
    with pytest.raises(ValueError, match="install.py"):
        pinning.validate(tmp_path, [])


class TypedRegistry:
    def __init__(self, host_type: str, features: tuple[str, ...] = ()) -> None:
        self.host_type = host_type
        self.features = features

    def get(self, host: str, key: str, default=None):
        if key == "config.type":
            return self.host_type
        if key == "pve-interface-pinning.interfaces":
            return ACE
        return default

    def has(self, host: str, feature: str) -> bool:
        return feature in self.features


@pytest.mark.parametrize("host_type", ["pve", "ubuntu"])
def test_validate_accepts_pve_and_debian_hosts(
    monkeypatch: pytest.MonkeyPatch, host_type: str
) -> None:
    monkeypatch.setattr(pinning, "default_registry", lambda _root: TypedRegistry(host_type))
    pinning.validate(REPO_ROOT, ["vm"])


def test_validate_rejects_other_host_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pinning, "default_registry", lambda _root: TypedRegistry("pbs"))
    with pytest.raises(ValueError, match="only supports pve/ubuntu hosts: vm"):
        pinning.validate(REPO_ROOT, ["vm"])


def test_validate_rejects_a_host_ubuntu_setup_already_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = TypedRegistry("ubuntu", features=("ubuntu-setup",))
    monkeypatch.setattr(pinning, "default_registry", lambda _root: registry)
    with pytest.raises(ValueError, match="conflicts with ubuntu-setup"):
        pinning.validate(REPO_ROOT, ["vm"])


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
        # VM-only reads: what `net_setup_link` reports per iface, and the initramfs.
        self.udev_props: dict[str, str] = {}
        self.initrd_listing = ""
        self.initramfs_code = 0

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        code = 0
        if command[:2] == ["udevadm", "test-builtin"]:
            iface = command[-1].rsplit("/", 1)[-1]
            return subprocess.CompletedProcess(command, 0, self.udev_props.get(iface, ""), "")
        if command[0] == "lsinitramfs":
            return subprocess.CompletedProcess(command, 0, self.initrd_listing, "")
        if command[0] == "update-initramfs":
            code = self.initramfs_code
        elif command[0] == "udevadm":
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
            and call[0] != "lsinitramfs"
            and call[:2] != ["udevadm", "test-builtin"]
        ]


class Host:
    """A sandboxed host filesystem plus a staged bundle rendered by the orchestrator."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pins, vm: bool = False
    ) -> None:
        self.vm = vm
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

        self.sys_net = self.root / "sys" / "class" / "net"
        monkeypatch.setattr(self.installer, "SYS_CLASS_NET", str(self.sys_net))
        self.boot = self.root / "boot"
        monkeypatch.setattr(self.installer, "BOOT_DIR", str(self.boot))
        self.purged: list[tuple[str, ...]] = []
        self.installed_packages: set[str] = set(pinning.CLOUD_PACKAGES) if vm else set()
        monkeypatch.setattr(self.installer.packages, "purge", self._purge)
        self.retired = self.etc_network / "10-lan.network"
        self.cloud_dir = self.root / "etc" / "cloud"
        self.cloud_drop_in = self.root / "etc" / "ssh" / "sshd_config.d" / "50-cloud-init.conf"
        self.env = (
            {
                "VM_GUEST": "true",
                "CLOUD_PACKAGES": " ".join(pinning.CLOUD_PACKAGES),
                "CLOUD_PATHS": f"{self.cloud_dir} {self.root / 'var/lib/cloud'} "
                f"{self.cloud_drop_in}",
                "RETIRED_NETWORK_FILES": str(self.retired),
            }
            if vm
            else {}
        )

        self.build_dir = tmp_path / "remote" / "build" / "h"
        self.force = False
        self.set_pins(pins)

    def _purge(self, ctx, *names: str) -> list[str]:
        present = [name for name in names if name in self.installed_packages]
        self.purged.append(tuple(names))
        self.installed_packages -= set(present)
        return present

    def nic(self, iface: str, pin, udev_name: str | None = None) -> None:
        """A NIC in sysfs; udev confirms `pin` for it unless `udev_name` differs."""
        path = self.sys_net / iface
        path.mkdir(parents=True)
        (path / "device").touch()
        (path / "address").write_text(f"{pin.mac}\n", encoding="utf-8")
        self.systemctl.udev_props[iface] = (
            f"ID_NET_LINK_FILE={self.dest(f'10-homelab-{pin.name}.link')}\n"
            f"ID_NET_LINK_FILE_DROPINS=\nID_NET_NAME={udev_name or pin.name}\n"
        )

    def initrd(self, *links: str) -> None:
        self.boot.mkdir(parents=True, exist_ok=True)
        (self.boot / "initrd.img-7.2.6+deb13-amd64").touch()
        self.systemctl.initrd_listing = "".join(
            f"usr/lib/systemd/network/{name}\n" for name in ("99-default.link", *links)
        )

    def cloud_leftovers(self) -> None:
        self.etc_network.mkdir(parents=True, exist_ok=True)
        self.retired.write_text("[Match]\nType=ether\nKind=!*\n", encoding="utf-8")
        (self.cloud_dir / "cloud.cfg.d").mkdir(parents=True)
        (self.cloud_dir / "cloud.cfg.d" / "90.cfg").write_text("x", encoding="utf-8")
        self.cloud_drop_in.parent.mkdir(parents=True)
        self.cloud_drop_in.write_text("PasswordAuthentication yes\n", encoding="utf-8")

    def set_pins(self, pins) -> None:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.build_dir.iterdir():
            stale.unlink()
        specs = []
        for pin in pins:
            name = f"10-homelab-{pin.name}.link"
            (self.build_dir / name).write_text(pinning.link_file(pin, "h"), encoding="utf-8")
            specs.append(pinning.FileSpec(name, f"/etc/systemd/network/{name}"))
            if self.vm:
                network = f"10-homelab-{pin.name}.network"
                (self.build_dir / network).write_text(
                    pinning.network_file(pin, "h"), encoding="utf-8"
                )
                specs.append(pinning.FileSpec(network, f"/etc/systemd/network/{network}"))
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
                env=dict(self.env),
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


# --- VM: .network, initramfs, decloud (homelab-ops#170) ---------------------------

VM_NIC = pinning.InterfacePin("nic0", "bc:24:11:18:e3:17", "management", False)
NETWORKD = "systemd-networkd.service"


@pytest.fixture
def vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    host = Host(tmp_path, monkeypatch, (VM_NIC,), vm=True)
    host.systemctl.enabled |= {NETWORKD}
    host.initrd()
    host.cloud_leftovers()
    return host


def test_a_confirmed_vm_gets_its_network_and_loses_cloud_init(vm: Host) -> None:
    vm.nic("ens18", VM_NIC)
    vm.deploy()

    network = vm.dest("10-homelab-nic0.network")
    assert network.read_text(encoding="utf-8") == pinning.network_file(VM_NIC, "h")
    assert "update-initramfs -u -k all" in vm.systemctl.actions()
    assert not vm.retired.exists()
    assert vm.purged == [pinning.CLOUD_PACKAGES]
    assert not vm.installed_packages
    assert not vm.cloud_dir.exists()
    assert not vm.cloud_drop_in.exists()


def test_networking_is_never_restarted(vm: Host) -> None:
    """Everything takes effect at the next boot (reboot rail)."""
    vm.nic("ens18", VM_NIC)
    vm.deploy()

    touched = " ".join(vm.systemctl.actions())
    assert "restart" not in touched and "reload systemd-networkd" not in touched
    assert not any(call[0] == "networkctl" for call in vm.systemctl.calls)


def test_networkd_is_enabled_for_the_next_boot_not_started(vm: Host) -> None:
    vm.systemctl.enabled.discard(NETWORKD)
    vm.nic("ens18", VM_NIC)
    vm.deploy()

    assert f"enable {NETWORKD}" in vm.systemctl.actions()
    assert f"enable --now {NETWORKD}" not in vm.systemctl.actions()


def test_an_unconfirmed_pin_keeps_cloud_init_and_the_old_network(
    vm: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """The .link would not rename the NIC: the retired file is its only match."""
    vm.nic("ens18", VM_NIC, udev_name="ens18")
    vm.deploy()

    assert vm.retired.exists()
    assert vm.purged == []
    assert vm.cloud_dir.exists()
    assert vm.dest("10-homelab-nic0.network").exists()
    out = capsys.readouterr().out
    assert "udev would not name ens18" in out
    assert "keeping cloud-init" in out


def test_a_pin_whose_mac_is_absent_is_unconfirmed(
    vm: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    vm.deploy()

    assert vm.retired.exists()
    assert vm.purged == []
    assert f"no NIC has MAC {VM_NIC.mac}" in capsys.readouterr().out


def test_a_nic_without_a_device_is_not_the_pinned_one(vm: Host) -> None:
    """A bridge or veth can share a MAC; only real devices count."""
    vm.nic("ens18", VM_NIC)
    (vm.sys_net / "ens18" / "device").unlink()
    vm.deploy()

    assert vm.purged == []


def test_a_converged_vm_rebuilds_nothing_and_says_so(
    vm: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    vm.nic("nic0", VM_NIC)
    vm.deploy()
    vm.initrd("10-homelab-nic0.link")
    capsys.readouterr()
    vm.deploy()

    assert vm.systemctl.actions() == []
    out = capsys.readouterr().out
    assert "initramfs already carries the pinned .link files" in out
    assert "cloud-init and netplan already gone" in out


def test_an_initramfs_without_the_link_is_rebuilt_on_an_unchanged_deploy(vm: Host) -> None:
    """riven's initramfs predates its .link; a plain redeploy converges it."""
    vm.nic("nic0", VM_NIC)
    vm.deploy()
    vm.deploy()

    assert vm.systemctl.actions() == ["update-initramfs -u -k all"]


def test_a_failed_initramfs_rebuild_fails_the_deploy_before_decloud(vm: Host) -> None:
    vm.nic("ens18", VM_NIC)
    vm.systemctl.initramfs_code = 1

    with pytest.raises(InstallError, match="--force"):
        vm.deploy()
    assert vm.purged == []
    assert vm.retired.exists()


def test_a_vm_map_without_network_files_refuses(vm: Host) -> None:
    vm.file_map = {n: v for n, v in vm.file_map.items() if not n.endswith(".network")}

    with pytest.raises(InstallError, match="no .network files"):
        vm.deploy()


def test_a_pve_node_runs_none_of_the_vm_steps(host_for) -> None:
    host = host_for()
    host.deploy()

    assert not any(
        call[0] in ("lsinitramfs", "update-initramfs") or call[:2] == ["udevadm", "test-builtin"]
        for call in host.systemctl.calls
    )
    assert host.purged == []
    assert not list(host.etc_network.glob("*.network"))
