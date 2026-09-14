"""ubuntu-setup: remote installer (freender/homelab-ops#30).

Every host path is rebound under `tmp_path` and every command is faked, so
nothing here touches a real sshd, sudoers, udev or apt. Both targets are offsite,
where a broken sshd or NIC name means Pi-KVM, so these pin:

* **Refusals come before writes.** An invalid staged sudoers file, a truncated
  env file or an unknown deploy user must leave the host exactly as it was --
  hostname included, which the bash set before `visudo` ran.
* **A converged host is not touched.** No hostname or timezone call, no
  relinked `/etc/localtime`, no rewritten udev rule, no sshd reload, no initramfs
  rebuild, no `sysctl --system`.
* **The effective sshd config is checked, not just the file.** sshd keeps the
  first value it reads, so an earlier drop-in silently wins over ours.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from homelab.modules import ubuntu_setup
from homelab_install import files, packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "ubuntu-setup" / "scripts" / "install.py"
MAC = "aa:bb:cc:00:00:01"
OTHER_MAC = "aa:bb:cc:00:00:02"
TZ = "America/New_York"
SSHD_HARDENING = (REPO_ROOT / "ubuntu-setup" / "configs" / "sshd-hardening.conf").read_text(
    encoding="utf-8"
)
SSHD_EFFECTIVE = (
    "port 22\n"
    "passwordauthentication no\n"
    "kbdinteractiveauthentication no\n"
    "pubkeyauthentication yes\n"
)


def load_installer():
    spec = importlib.util.spec_from_file_location("ubuntu_setup_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    """Answers by longest matching argv prefix; records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], tuple[int, str]] = {}

    def answer(self, *argv: str, code: int = 0, stdout: str = "") -> None:
        self.answers[argv] = (code, stdout)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        for length in range(len(argv), 0, -1):
            if tuple(argv[:length]) in self.answers:
                code, stdout = self.answers[tuple(argv[:length])]
                break
        else:
            code, stdout = 0, ""
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code, stdout=stdout)

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


class Host:
    """A converged offsite host: every managed file already matches the build."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "host"
        self.build = tmp_path / "stage" / "build" / "cinci"
        self.build.mkdir(parents=True)

        paths = {
            "ZONEINFO": self.root / "usr/share/zoneinfo",
            "LOCALTIME": self.root / "etc/localtime",
            "ETC_TIMEZONE": self.root / "etc/timezone",
            "SYS_CLASS_NET": self.root / "sys/class/net",
            "UDEV_RULES_DIR": self.root / "etc/udev/rules.d",
            "LEGACY_PERSISTENT_NET": self.root / "etc/udev/rules.d/70-persistent-net.rules",
            "NETPLAN_DIR": self.root / "etc/netplan",
            "ZFS_ARC_MAX_PARAM": self.root / "sys/module/zfs/parameters/zfs_arc_max",
            "WIREGUARD_DIR": self.root / "etc/wireguard",
        }
        for name, path in paths.items():
            monkeypatch.setattr(self.installer, name, str(path))
        self.paths = paths
        monkeypatch.setattr(
            self.installer,
            "DOCKER_APT_SOURCES",
            (str(self.root / "etc/apt/sources.list.d/docker.list"),),
        )

        self.run = FakeRun()
        for target in (self.installer, files, systemd, packages):
            monkeypatch.setattr(target, "_run", self.run)
        self.binaries = {"docker"}
        monkeypatch.setattr(
            self.installer,
            "_which",
            lambda name: f"/usr/bin/{name}" if name in self.binaries else None,
        )
        self.users = {"sysadm": SimpleNamespace(pw_gid=1000)}
        self.docker_group = SimpleNamespace(gr_mem=["sysadm"], gr_gid=983)
        monkeypatch.setattr(self.installer, "pwd", SimpleNamespace(getpwnam=self._getpwnam))
        monkeypatch.setattr(self.installer, "grp", SimpleNamespace(getgrnam=self._getgrnam))

        self.env = {
            "DEPLOY_USER": "sysadm",
            "PRIMARY_INTERFACE_NAME": "nic0",
            "PRIMARY_INTERFACE_MAC": MAC,
            "SYSTEM_HOSTNAME": "cinci",
            "SYSTEM_TIMEZONE": TZ,
            "WIREGUARD_ENABLED": "false",
            "ZFS_ARC_MAX": "8589934592",
        }
        self.file_map: dict[str, tuple[str, str]] = {}
        self.render(
            "sudoers",
            "sysadm ALL=(ALL:ALL) NOPASSWD: /usr/sbin/reboot\n",
            "sudoers.d/99-sysadm-homelab",
            "440",
        )
        self.render("10-network-names.rules", self.rule(MAC), "udev/rules.d/10-network-names.rules")
        self.render(
            "sshd-hardening.conf", SSHD_HARDENING, "ssh/sshd_config.d/99-disable-password-auth.conf"
        )
        self.render("zfs.conf", "options zfs zfs_arc_max=8589934592\n", "modprobe.d/zfs.conf")
        self.render(
            "99-inotify.conf", "fs.inotify.max_user_watches=1048576\n", "sysctl.d/99-inotify.conf"
        )

        zone = paths["ZONEINFO"] / TZ
        zone.parent.mkdir(parents=True)
        zone.write_text("TZif\n", encoding="utf-8")
        paths["LOCALTIME"].symlink_to(zone)
        paths["ETC_TIMEZONE"].write_text(f"{TZ}\n", encoding="utf-8")
        self.nic("nic0", MAC)
        self.nic("wlp2s0", "aa:bb:cc:00:00:ff", kind="1", carrier=None)
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        (self.root / "etc/apt/sources.list.d/docker.list").write_text("deb x\n", encoding="utf-8")
        paths["ZFS_ARC_MAX_PARAM"].parent.mkdir(parents=True)
        paths["ZFS_ARC_MAX_PARAM"].write_text("8589934592\n", encoding="utf-8")

        self.run.answer("hostnamectl", "status", "--static", stdout="cinci\n")
        self.run.answer("timedatectl", "show", stdout=f"{TZ}\n")
        self.run.answer(
            "ip", "route", "show", "default", stdout="default via 10.0.0.1 dev nic0 proto dhcp\n"
        )
        self.run.answer("systemctl", "is-enabled", "openipmi.service", stdout="masked\n")
        self.run.answer("systemctl", "is-active", "--quiet", "NetworkManager", code=3)
        self.run.answer("sshd", "-T", stdout=SSHD_EFFECTIVE)
        self.run.answer("dpkg-query", stdout="install ok installed")
        self.script_dir = tmp_path / "stage"

    def _getpwnam(self, name: str):
        if name not in self.users:
            raise KeyError(name)
        return self.users[name]

    def _getgrnam(self, name: str):
        if name != "docker" or self.docker_group is None:
            raise KeyError(name)
        return self.docker_group

    @staticmethod
    def rule(mac: str) -> str:
        return (
            "# Pin ethernet interface to nic0\n"
            f'SUBSYSTEM=="net", ACTION=="add", ATTR{{address}}=="{mac}", NAME="nic0"\n'
        )

    def render(self, name: str, content: str, etc_path: str, mode: str = "644") -> None:
        (self.build / name).write_text(content, encoding="utf-8")
        dest = self.root / "etc" / etc_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        self.file_map[name] = (str(dest), mode)

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def nic(self, name: str, mac: str, kind: str = "1", carrier: str | None = "1") -> None:
        iface = self.paths["SYS_CLASS_NET"] / name
        iface.mkdir(parents=True)
        (iface / "address").write_text(f"{mac}\n", encoding="utf-8")
        (iface / "type").write_text(f"{kind}\n", encoding="utf-8")
        if carrier is not None:
            (iface / "carrier").write_text(f"{carrier}\n", encoding="utf-8")

    def install(self, force: bool = False) -> None:
        ctx = InstallContext(
            host="cinci",
            script_dir=self.script_dir,
            build_dir=self.build,
            env=dict(self.env),
            deploy_env={},
            file_map=dict(self.file_map),
            force_update=force,
        )
        self.installer.install(ctx)

    def snapshot(self) -> dict[str, tuple[int, bytes]]:
        return {
            str(path): (path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else b"")
            for path in sorted((self.root / "etc").rglob("*"))
            if path.is_file() or path.is_symlink()
        }


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    monkeypatch.setattr(packages, "_apt_updated", False)
    return Host(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Converged host
# ---------------------------------------------------------------------------


def test_a_converged_host_is_not_touched(host: Host) -> None:
    before = host.snapshot()

    host.install()

    assert host.snapshot() == before
    for mutating in (
        ("hostnamectl", "set-hostname"),
        ("timedatectl", "set-timezone"),
        ("systemctl", "mask"),
        ("systemctl", "reload"),
        ("sshd", "-t"),
        ("update-initramfs",),
        ("sysctl",),
        ("usermod",),
        ("curl",),
        ("apt-get",),
    ):
        assert not host.run.ran(*mutating), mutating
    assert host.run.ran("sshd", "-T")


def test_the_real_orchestrator_file_specs_cover_every_file_the_installer_needs() -> None:
    """The installer refuses a bundle missing any of these, so the orchestrator
    must render every one -- a rename on either side would fail every deploy."""
    installer = load_installer()
    rendered = {spec.build_name for spec in ubuntu_setup.FILE_SPECS}

    assert set(installer.REQUIRED_BUILD_FILES) | {installer.WIREGUARD_SYSCTL} == rendered
    assert installer.SSHD_HARDENING in rendered


# ---------------------------------------------------------------------------
# Preflight refusals
# ---------------------------------------------------------------------------


def _assert_untouched(host: Host, before: dict[str, tuple[int, bytes]]) -> None:
    assert host.snapshot() == before
    assert not host.run.ran("hostnamectl", "set-hostname")
    assert not host.run.ran("timedatectl", "set-timezone")


def test_an_invalid_staged_sudoers_file_is_refused_before_anything_is_written(host: Host) -> None:
    host.env["SYSTEM_HOSTNAME"] = "renamed"
    host.run.answer("visudo", "-cf", code=1)
    before = host.snapshot()

    with pytest.raises(InstallError, match="staged sudoers file is invalid"):
        host.install()

    _assert_untouched(host, before)


@pytest.mark.parametrize(
    "key", ["DEPLOY_USER", "SYSTEM_TIMEZONE", "WIREGUARD_ENABLED", "PRIMARY_INTERFACE_MAC"]
)
def test_a_truncated_env_file_is_refused(host: Host, key: str) -> None:
    del host.env[key]
    before = host.snapshot()

    with pytest.raises(InstallError, match=key):
        host.install()

    _assert_untouched(host, before)


def test_an_empty_primary_mac_is_allowed_and_pins_by_fallback(host: Host) -> None:
    host.env["PRIMARY_INTERFACE_MAC"] = ""
    (host.build / "10-network-names.rules").write_text(host.rule(""), encoding="utf-8")

    host.install()

    assert host.dest("10-network-names.rules").read_text(encoding="utf-8") == host.rule(MAC)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda h: h.env.update(WIREGUARD_ENABLED="ture"),
            "WIREGUARD_ENABLED must be true or false",
        ),
        (lambda h: h.env.update(ZFS_ARC_MAX="8G"), "ZFS_ARC_MAX must be a byte count"),
        (lambda h: h.env.update(SYSTEM_TIMEZONE="Mars/Olympus"), "timezone data not found"),
        (
            lambda h: h.env.update(DEPLOY_USER="nobody-here"),
            "deploy user nobody-here does not exist",
        ),
        (lambda h: h.env.update(WIREGUARD_ENABLED="true"), r"missing: 99-wireguard\.conf"),
        (lambda h: (h.build / "zfs.conf").unlink(), r"missing: zfs\.conf"),
    ],
)
def test_bad_input_is_refused_before_anything_is_written(host: Host, change, message: str) -> None:
    host.env["SYSTEM_HOSTNAME"] = "renamed"
    change(host)
    before = host.snapshot()

    with pytest.raises(InstallError, match=message):
        host.install()

    _assert_untouched(host, before)
    assert not host.run.ran("visudo")


# ---------------------------------------------------------------------------
# Hostname and timezone
# ---------------------------------------------------------------------------


def test_hostname_and_timezone_are_set_only_when_they_differ(host: Host) -> None:
    host.run.answer("hostnamectl", "status", "--static", stdout="ubuntu\n")
    host.run.answer("timedatectl", "show", stdout="Etc/UTC\n")

    host.install()

    assert ["hostnamectl", "set-hostname", "cinci"] in host.run.calls
    assert ["timedatectl", "set-timezone", TZ] in host.run.calls


def test_a_stale_localtime_link_and_timezone_file_are_corrected(host: Host) -> None:
    localtime = Path(host.paths["LOCALTIME"])
    localtime.unlink()
    localtime.symlink_to("/usr/share/zoneinfo/Etc/UTC")
    host.paths["ETC_TIMEZONE"].write_text("Etc/UTC\n", encoding="utf-8")

    host.install()

    assert str(localtime.readlink()) == str(host.paths["ZONEINFO"] / TZ)
    assert host.paths["ETC_TIMEZONE"].read_text(encoding="utf-8") == f"{TZ}\n"
    assert not list(localtime.parent.glob(".localtime*"))


def test_a_timedatectl_failure_fails_the_deploy(host: Host) -> None:
    host.run.answer("timedatectl", "show", stdout="Etc/UTC\n")
    host.run.answer("timedatectl", "set-timezone", code=1)

    with pytest.raises(InstallError, match="timedatectl set-timezone"):
        host.install()


# ---------------------------------------------------------------------------
# Unwanted units
# ---------------------------------------------------------------------------


def test_an_unmasked_openipmi_is_masked(host: Host) -> None:
    host.run.answer("systemctl", "is-enabled", "openipmi.service", stdout="enabled\n")

    host.install()

    assert ["systemctl", "mask", "openipmi.service"] in host.run.calls


# ---------------------------------------------------------------------------
# Primary NIC pinning
# ---------------------------------------------------------------------------


def test_a_drifted_nic_rule_is_restored_with_a_reboot_warning(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.dest("10-network-names.rules").write_text(host.rule(OTHER_MAC), encoding="utf-8")

    host.install()

    assert host.dest("10-network-names.rules").read_text(encoding="utf-8") == host.rule(MAC)
    assert "reboot may be required" in capsys.readouterr().out


def test_force_rewriting_an_identical_nic_rule_does_not_warn_about_reboot(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.install(force=True)

    assert "reboot may be required" not in capsys.readouterr().out


def test_a_mac_differing_only_in_case_is_the_preferred_mac(host: Host) -> None:
    host.env["PRIMARY_INTERFACE_MAC"] = MAC.upper()
    rule = host.rule(MAC.upper())
    (host.build / "10-network-names.rules").write_text(rule, encoding="utf-8")

    host.install()

    assert host.dest("10-network-names.rules").read_text(encoding="utf-8") == rule


def test_a_preferred_mac_not_on_the_host_pins_the_default_route_link(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.env["PRIMARY_INTERFACE_MAC"] = OTHER_MAC
    (host.build / "10-network-names.rules").write_text(host.rule(OTHER_MAC), encoding="utf-8")

    host.install()

    assert host.dest("10-network-names.rules").read_text(encoding="utf-8") == host.rule(MAC)
    assert f"primary MAC {OTHER_MAC} not on this host; pinning nic0" in capsys.readouterr().out


def test_fallback_skips_a_default_route_that_is_not_ethernet(host: Host) -> None:
    host.run.answer("ip", "route", "show", "default", stdout="default dev wg0 scope link\n")
    host.nic("wg0", "00:00:00:00:00:00", kind="65534")
    host.env["PRIMARY_INTERFACE_MAC"] = ""
    (host.build / "10-network-names.rules").write_text(host.rule(""), encoding="utf-8")

    host.install()

    assert host.dest("10-network-names.rules").read_text(encoding="utf-8") == host.rule(MAC)


def test_the_default_route_device_is_read_from_after_dev(host: Host) -> None:
    """awk's `$5` is `link` for this route, not the device."""
    host.run.answer("ip", "route", "show", "default", stdout="default dev wg0 scope link\n")

    assert host.installer.default_route_iface() == "wg0"


def test_no_default_route_yields_no_device(host: Host) -> None:
    host.run.answer("ip", "route", "show", "default", stdout="")

    assert host.installer.default_route_iface() == ""


def test_no_usable_link_warns_and_leaves_the_rule_alone(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.env["PRIMARY_INTERFACE_MAC"] = OTHER_MAC
    (host.paths["SYS_CLASS_NET"] / "nic0" / "carrier").write_text("0\n", encoding="utf-8")
    before = host.dest("10-network-names.rules").read_bytes()

    host.install()

    assert host.dest("10-network-names.rules").read_bytes() == before
    assert "Could not determine a primary ethernet interface" in capsys.readouterr().out


def test_a_template_the_installer_cannot_rewrite_is_refused(host: Host) -> None:
    host.env["PRIMARY_INTERFACE_MAC"] = OTHER_MAC

    with pytest.raises(InstallError, match="template and installer disagree"):
        host.install()


def test_competing_naming_mechanisms_warn(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    rules = host.paths["UDEV_RULES_DIR"]
    (rules / "70-persistent-net.rules").write_text("# legacy\n", encoding="utf-8")
    (rules / "80-rename.rules").write_text(
        f'ATTR{{address}}=="{MAC.upper()}", NAME="eth9"\n', encoding="utf-8"
    )
    host.paths["NETPLAN_DIR"].mkdir(parents=True)
    (host.paths["NETPLAN_DIR"] / "50-cloud-init.yaml").write_text(
        f"macaddress: {MAC}\n", encoding="utf-8"
    )
    host.binaries.add("nmcli")
    host.run.answer("systemctl", "is-active", "--quiet", "NetworkManager", code=0)
    host.run.answer("nmcli", stdout=f"GENERAL.HWADDR:{MAC.upper().replace(':', chr(92) + ':')}\n")

    host.install()

    out = capsys.readouterr().out
    assert "legacy" in out and "70-persistent-net.rules present" in out
    assert "competing udev naming rule" in out and "80-rename.rules" in out
    assert "50-cloud-init.yaml also references" in out
    assert "NetworkManager is active" in out


def test_a_rule_that_only_sets_an_env_name_is_not_competing(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = host.paths["UDEV_RULES_DIR"]
    (rules / "80-env.rules").write_text(
        f'ATTR{{address}}=="{MAC}", ENV{{ID_NET_NAME}}="x"\n', encoding="utf-8"
    )
    (rules / "81-split.rules").write_text(
        f'ATTR{{address}}=="{MAC}"\nNAME="elsewhere"\n', encoding="utf-8"
    )

    host.install()

    assert "competing udev naming rule" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Docker CE
# ---------------------------------------------------------------------------


def test_missing_docker_runs_the_convenience_installer(host: Host) -> None:
    host.binaries.discard("docker")

    host.install()

    assert host.run.ran("curl", "-fsSL", "https://get.docker.com", "-o")
    sh = next(call for call in host.run.calls if call[0] == "sh")
    assert len(sh) == 2 and sh[1].endswith("get-docker.sh")
    assert not Path(sh[1]).exists()


def test_a_missing_docker_apt_source_only_sets_up_the_repo(host: Host) -> None:
    (host.root / "etc/apt/sources.list.d/docker.list").unlink()

    host.install()

    sh = next(call for call in host.run.calls if call[0] == "sh")
    assert sh[2:] == ["--setup-repo"]


def test_a_failed_docker_download_fails_the_deploy(host: Host) -> None:
    host.binaries.discard("docker")
    host.run.answer("curl", code=22)

    with pytest.raises(InstallError, match="curl -fsSL .* failed"):
        host.install()
    assert not any(call[0] == "sh" for call in host.run.calls)


def test_snap_docker_is_removed_first(host: Host) -> None:
    host.binaries.add("snap")

    host.install()

    assert ["snap", "remove", "--purge", "docker"] in host.run.calls


def test_no_snap_docker_is_left_alone(host: Host) -> None:
    host.binaries.add("snap")
    host.run.answer("snap", "list", "docker", code=1)

    host.install()

    assert not host.run.ran("snap", "remove")


def test_the_deploy_user_is_added_to_the_docker_group(host: Host) -> None:
    host.docker_group.gr_mem = []

    host.install()

    assert ["usermod", "-aG", "docker", "sysadm"] in host.run.calls


def test_a_primary_docker_group_counts_as_membership(host: Host) -> None:
    host.docker_group.gr_mem = []
    host.users["sysadm"].pw_gid = host.docker_group.gr_gid

    host.install()

    assert not host.run.ran("usermod")


def test_a_missing_docker_group_fails_the_deploy(host: Host) -> None:
    host.docker_group = None

    with pytest.raises(InstallError, match="docker group does not exist"):
        host.install()


# ---------------------------------------------------------------------------
# Sudoers and sshd
# ---------------------------------------------------------------------------


def test_drifted_sudoers_is_restored_at_440(host: Host) -> None:
    dest = host.dest("sudoers")
    dest.write_text("sysadm ALL=(ALL) NOPASSWD: ALL\n", encoding="utf-8")
    dest.chmod(0o644)

    host.install()

    assert dest.read_bytes() == (host.build / "sudoers").read_bytes()
    assert dest.stat().st_mode & 0o777 == 0o440
    assert not list(dest.parent.glob("*.bak.*"))


def test_a_changed_sshd_drop_in_is_validated_then_reloaded(host: Host) -> None:
    host.dest("sshd-hardening.conf").write_text("PasswordAuthentication yes\n", encoding="utf-8")

    host.install()

    order = [call[:2] for call in host.run.calls if call[0] in ("sshd", "systemctl")]
    assert (
        order.index(["sshd", "-t"])
        < order.index(["systemctl", "reload"])
        < order.index(["sshd", "-T"])
    )
    assert ["systemctl", "reload", "ssh"] in host.run.calls


def test_a_rejected_sshd_drop_in_is_rolled_back_and_never_reloaded(host: Host) -> None:
    dest = host.dest("sshd-hardening.conf")
    dest.write_text("PasswordAuthentication yes\n", encoding="utf-8")
    host.run.answer("sshd", "-t", code=255)

    with pytest.raises(InstallError, match="rolled back"):
        host.install()

    assert dest.read_text(encoding="utf-8") == "PasswordAuthentication yes\n"
    assert not host.run.ran("systemctl", "reload")


def test_an_earlier_drop_in_overriding_ours_fails_the_deploy(host: Host) -> None:
    """cloud-init's 50-cloud-init.conf sorts ahead of 99-, and sshd keeps the first value."""
    host.run.answer(
        "sshd",
        "-T",
        stdout=SSHD_EFFECTIVE.replace("passwordauthentication no", "passwordauthentication yes"),
    )

    with pytest.raises(InstallError, match="passwordauthentication is yes, expected no"):
        host.install()


def test_the_deprecated_challenge_response_keyword_is_checked_as_kbdinteractive(host: Host) -> None:
    expected = host.installer.expected_sshd_settings(host.build / "sshd-hardening.conf")

    assert "challengeresponseauthentication" not in expected
    assert expected["kbdinteractiveauthentication"] == "no"
    host.run.answer(
        "sshd", "-T", stdout=SSHD_EFFECTIVE.replace("kbdinteractiveauthentication no\n", "")
    )
    with pytest.raises(InstallError, match=r"kbdinteractiveauthentication is \(unset\)"):
        host.install()


# ---------------------------------------------------------------------------
# ZFS ARC, sysctl, WireGuard
# ---------------------------------------------------------------------------


def test_a_changed_arc_limit_rebuilds_the_initramfs_and_applies_it_live(host: Host) -> None:
    host.env["ZFS_ARC_MAX"] = "4294967296"
    (host.build / "zfs.conf").write_text("options zfs zfs_arc_max=4294967296\n", encoding="utf-8")

    host.install()

    assert ["update-initramfs", "-u"] in host.run.calls
    assert host.paths["ZFS_ARC_MAX_PARAM"].read_text(encoding="utf-8") == "4294967296\n"


def test_a_failed_initramfs_rebuild_names_force_as_the_retry(host: Host) -> None:
    host.dest("zfs.conf").write_text("stale\n", encoding="utf-8")
    host.run.answer("update-initramfs", code=1)

    with pytest.raises(InstallError, match="--force"):
        host.install()


def test_an_unloaded_zfs_module_fails_the_live_arc_write(host: Host) -> None:
    host.dest("zfs.conf").write_text("stale\n", encoding="utf-8")
    host.paths["ZFS_ARC_MAX_PARAM"].unlink()
    host.paths["ZFS_ARC_MAX_PARAM"].parent.rmdir()

    with pytest.raises(InstallError, match="could not set"):
        host.install()


def test_changed_inotify_limits_reload_sysctl(host: Host) -> None:
    host.dest("99-inotify.conf").write_text("stale\n", encoding="utf-8")

    host.install()

    assert ["sysctl", "--system"] in host.run.calls


def test_wireguard_installs_packages_sysctl_and_starts_every_tunnel(host: Host) -> None:
    host.env["WIREGUARD_ENABLED"] = "true"
    (host.build / "99-wireguard.conf").write_text("net.ipv4.ip_forward=1\n", encoding="utf-8")
    host.file_map["99-wireguard.conf"] = (str(host.root / "etc/sysctl.d/99-wireguard.conf"), "644")
    wg = host.paths["WIREGUARD_DIR"]
    wg.mkdir(parents=True)
    (wg / "wg0.conf").write_text("", encoding="utf-8")
    host.run.answer("dpkg-query", code=1)
    host.run.answer("systemctl", "is-enabled", "--quiet", "wg-quick@wg0.service", code=1)

    with pytest.raises(InstallError, match="still missing"):
        host.install()
    assert ["apt-get", "install", "-y", "-q", "wireguard", "wireguard-tools"] in host.run.calls

    host.run.answer("dpkg-query", stdout="install ok installed")
    host.install()

    assert ["sysctl", "--system"] in host.run.calls
    assert ["systemctl", "enable", "--now", "wg-quick@wg0.service"] in host.run.calls


def test_an_unreadable_hostname_fails_rather_than_renaming(host: Host) -> None:
    host.run.answer("hostnamectl", "status", "--static", code=1)

    with pytest.raises(InstallError, match="hostnamectl status --static failed"):
        host.install()
    assert not host.run.ran("hostnamectl", "set-hostname")
