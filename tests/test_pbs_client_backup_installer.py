"""Remote installer for pbs-client-backup (freender/homelab-ops#30).

Every absolute destination is rebound under `tmp_path`, and the subprocess surfaces
-- `packages._run` (dpkg/apt), `systemd._run` (systemctl), and the installer's own
`_run` and `_which` -- are faked. Nothing here touches `/etc`, a real unit, or apt.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "pbs-client-backup" / "scripts" / "install.py"

SERVICE = "homelab-pbs-client-backup.service"
TIMER = "homelab-pbs-client-backup.timer"
FILE_MAP = (
    ("homelab-pbs-client-backup", "/usr/local/sbin/homelab-pbs-client-backup", "700"),
    ("homelab-pbs-client-backup.conf", "/etc/homelab/pbs-client-backup.conf", "600"),
    (SERVICE, f"/etc/systemd/system/{SERVICE}", "644"),
    (TIMER, f"/etc/systemd/system/{TIMER}", "644"),
)
SOURCE = (
    "Types: deb\n"
    "URIs: http://download.proxmox.com/debian/pbs-client\n"
    "Suites: trixie\n"
    "Components: main\n"
    "Signed-By: {keyring}\n"
)


def load_installer():
    """Fresh load per test: these rebind the module's destination constants."""
    spec = importlib.util.spec_from_file_location("pbs_client_backup_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApt:
    """Every package installed unless named in `missing`; installing fixes it
    unless `broken`."""

    def __init__(self, missing: set[str] | None = None, broken: bool = False) -> None:
        self.missing = set(missing or ())
        self.broken = broken
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "dpkg-query":
            if command[-1] in self.missing:
                return subprocess.CompletedProcess(command, 1, stdout="")
            return subprocess.CompletedProcess(command, 0, stdout="install ok installed")
        if command[:2] == ["apt-get", "install"] and not self.broken:
            self.missing -= set(command[4:])
        return subprocess.CompletedProcess(command, 0, stdout="")

    @property
    def apt_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "apt-get"]


class FakeSystemctl:
    def __init__(self, enabled: set[str], active: set[str]) -> None:
        self.enabled = enabled
        self.active = active
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb = command[1]
        if verb == "is-enabled":
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.enabled else 1)
        if verb == "is-active":
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.active else 1)
        return subprocess.CompletedProcess(command, 0)

    def actions(self) -> list[str]:
        return [
            " ".join(call[1:]) for call in self.calls if call[1] not in {"is-enabled", "is-active"}
        ]


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.sandbox = tmp_path / "hostfs"
        self.installer = load_installer()
        for name in ("DESTINATION_ENV", "OS_RELEASE", "KEYRING_DIR", "PBS_CLIENT_SOURCE"):
            setattr(self.installer, name, self.under(getattr(self.installer, name)))

        self.script_dir = tmp_path / "remote"
        self.build_dir = self.script_dir / "build" / "host"
        self.build_dir.mkdir(parents=True)
        for name, _dest, _mode in FILE_MAP:
            (self.build_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        self.file_map = {name: (self.under(dest), mode) for name, dest, mode in FILE_MAP}
        keyrings = self.script_dir / "configs" / "keyrings"
        keyrings.mkdir(parents=True)
        (keyrings / "proxmox-release-trixie.gpg").write_bytes(b"trixie keyring")
        self.write(
            self.installer.OS_RELEASE, 'ID=ubuntu\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\n'
        )

        self.keyfile = self.under("/etc/homelab/pbs-encryption.key")
        self.env = {
            "HOST_TYPE": "pve",
            "DESTINATION_COUNT": "1",
            "NEEDS_ZFS": "false",
            "ENCRYPT": "false",
            "PURGE_KEYFILE": "false",
            "KEYFILE": self.keyfile,
            "PAUSED": "false",
        }
        self.stage_destinations(1)
        self.binaries = {"proxmox-backup-client", "zfs"}
        self.apt = FakeApt()
        self.systemctl = FakeSystemctl(enabled={TIMER}, active={TIMER})
        self.host_calls: list[list[str]] = []
        self.force = False

    def under(self, dest: str) -> str:
        return str(self.sandbox / dest.lstrip("/"))

    @staticmethod
    def write(path: str, content: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def destination(self, index: int) -> Path:
        return Path(self.installer.DESTINATION_ENV.format(index=index))

    def stage_destinations(self, count: int) -> None:
        self.env["DESTINATION_COUNT"] = str(count)
        for index in range(count):
            (self.build_dir / f"destination-{index}.env").write_text(
                f"PBS_PASSWORD=secret-{index}\n", encoding="utf-8"
            )

    def ubuntu(self) -> None:
        """A converged Ubuntu host: keyring and source in place."""
        self.env["HOST_TYPE"] = "ubuntu"
        keyring = f"{self.installer.KEYRING_DIR}/proxmox-release-trixie.gpg"
        Path(keyring).parent.mkdir(parents=True, exist_ok=True)
        Path(keyring).write_bytes(b"trixie keyring")
        self.write(self.installer.PBS_CLIENT_SOURCE, SOURCE.format(keyring=keyring))

    def _host(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.host_calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="client version: 4.2.5\n")

    def _which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.binaries else None

    def run(self, lists_fresh: bool = False) -> InstallContext:
        """`lists_fresh` models an `apt-get update` that already ran this process."""
        self.monkeypatch.setattr(packages, "_run", self.apt)
        self.monkeypatch.setattr(packages, "_apt_updated", lists_fresh)
        self.monkeypatch.setattr(systemd, "_run", self.systemctl)
        self.monkeypatch.setattr(self.installer, "_run", self._host)
        self.monkeypatch.setattr(self.installer, "_which", self._which)
        ctx = InstallContext(
            host="host",
            script_dir=self.script_dir,
            build_dir=self.build_dir,
            env=dict(self.env),
            deploy_env={},
            file_map=dict(self.file_map),
            force_update=self.force,
        )
        self.installer.install(ctx)
        return ctx

    def converge(self) -> None:
        self.run()
        self.apt.calls.clear()
        self.systemctl.calls.clear()
        self.host_calls.clear()

    def written(self) -> list[Path]:
        """Files the installer could have written: everything but the seeded os-release."""
        seeded = Path(self.installer.OS_RELEASE)
        return sorted(p for p in self.sandbox.rglob("*") if p.is_file() and p != seeded)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# converged hosts
# --------------------------------------------------------------------------


def test_a_converged_pve_redeploy_changes_nothing(harness: Harness) -> None:
    harness.converge()
    mtimes = {path: path.stat().st_mtime_ns for path in harness.written()}

    ctx = harness.run()

    assert not ctx.changes.any()
    assert {path: path.stat().st_mtime_ns for path in harness.written()} == mtimes
    assert harness.apt.calls == []
    assert harness.systemctl.actions() == []


def test_a_converged_ubuntu_redeploy_touches_neither_apt_nor_the_repo(harness: Harness) -> None:
    harness.ubuntu()
    harness.converge()

    ctx = harness.run()

    assert not ctx.changes.any()
    assert harness.apt.apt_calls == []
    assert harness.systemctl.actions() == []


def test_first_install_writes_every_file_with_its_mode_and_enables_the_timer(
    harness: Harness,
) -> None:
    harness.systemctl.enabled.clear()
    harness.systemctl.active.clear()

    harness.run()

    for name, _dest, mode in FILE_MAP:
        assert harness.dest(name).read_text(encoding="utf-8") == f"# {name}\n"
        assert oct(harness.dest(name).stat().st_mode & 0o777)[2:] == mode
    assert harness.destination(0).stat().st_mode & 0o777 == 0o600
    assert harness.systemctl.actions() == [
        "daemon-reload",
        f"reset-failed {SERVICE}",
        "daemon-reload",
        f"enable --now {TIMER}",
    ]
    assert ["systemctl", "list-timers", TIMER, "--no-pager", "--all"] in harness.host_calls


# --------------------------------------------------------------------------
# refusals: every one of them before any write
# --------------------------------------------------------------------------


def _refuses(harness: Harness, match: str) -> None:
    with pytest.raises(InstallError, match=match):
        harness.run()
    assert harness.written() == []
    assert harness.systemctl.calls == []
    assert harness.apt.apt_calls == []


@pytest.mark.parametrize("key", load_installer().REQUIRED_ENV)
def test_a_missing_env_key_refuses(harness: Harness, key: str) -> None:
    """The bash defaulted every one of these, so a truncated conf read as unencrypted
    and not paused."""
    del harness.env[key]
    _refuses(harness, f"missing: {key}")


def test_an_unsupported_host_type_refuses(harness: Harness) -> None:
    harness.env["HOST_TYPE"] = "debian"
    _refuses(harness, "unsupported HOST_TYPE 'debian'")


@pytest.mark.parametrize("count", ["0", "abc", "-1"])
def test_a_bad_destination_count_refuses(harness: Harness, count: str) -> None:
    harness.env["DESTINATION_COUNT"] = count
    _refuses(harness, "DESTINATION_COUNT must be a positive integer")


@pytest.mark.parametrize("key", ["ENCRYPT", "PURGE_KEYFILE", "PAUSED", "NEEDS_ZFS"])
def test_a_flag_typo_refuses_instead_of_reading_as_false(harness: Harness, key: str) -> None:
    harness.env[key] = "ture"
    _refuses(harness, f"{key} must be true or false")


def test_a_missing_staged_destination_refuses(harness: Harness) -> None:
    harness.env["DESTINATION_COUNT"] = "2"
    _refuses(harness, "destination-1.env")


def test_a_missing_build_file_refuses(harness: Harness) -> None:
    (harness.build_dir / TIMER).unlink()
    _refuses(harness, f"missing staged files: .*{TIMER}")


def test_a_missing_file_map_entry_refuses(harness: Harness) -> None:
    del harness.file_map[SERVICE]
    _refuses(harness, f"missing file-map entries: {SERVICE}")


def test_encryption_without_a_staged_keyfile_refuses_and_says_how_to_stage_it(
    harness: Harness,
) -> None:
    harness.env["ENCRYPT"] = "true"
    _refuses(harness, "pbs-encryption.key; run ./deploy pbs-client-backup host from riven")


def test_pve_without_the_client_refuses(harness: Harness) -> None:
    harness.binaries.discard("proxmox-backup-client")
    _refuses(harness, "proxmox-backup-client not found")


def test_a_zfs_archive_without_zfs_refuses(harness: Harness) -> None:
    harness.env["NEEDS_ZFS"] = "true"
    harness.binaries.discard("zfs")
    _refuses(harness, "zfs command is not found")


def test_no_zfs_is_fine_when_no_archive_needs_it(harness: Harness) -> None:
    harness.binaries.discard("zfs")
    harness.run()


def test_an_unmapped_ubuntu_release_refuses(harness: Harness) -> None:
    harness.env["HOST_TYPE"] = "ubuntu"
    harness.write(harness.installer.OS_RELEASE, "VERSION_ID=22.04\nVERSION_CODENAME=jammy\n")
    with pytest.raises(InstallError, match=r"unable to map this Ubuntu release \(22.04 jammy\)"):
        harness.run()
    assert harness.systemctl.calls == []


def test_a_missing_vendored_keyring_refuses(harness: Harness) -> None:
    harness.env["HOST_TYPE"] = "ubuntu"
    harness.write(harness.installer.OS_RELEASE, "VERSION_ID=24.04\n")
    with pytest.raises(InstallError, match="missing vendored keyring: .*bookworm"):
        harness.run()
    assert not Path(harness.installer.PBS_CLIENT_SOURCE).exists()


def test_the_suite_falls_back_to_the_codename(harness: Harness) -> None:
    harness.write(harness.installer.OS_RELEASE, "VERSION_ID=26.10\nVERSION_CODENAME=resolute # c\n")
    assert harness.installer.ubuntu_suite() == "trixie"


def test_no_os_release_is_unmapped(harness: Harness) -> None:
    Path(harness.installer.OS_RELEASE).unlink()
    with pytest.raises(InstallError, match=r"\(\? \?\)"):
        harness.installer.ubuntu_suite()


# --------------------------------------------------------------------------
# Ubuntu client install
# --------------------------------------------------------------------------


def test_a_fresh_ubuntu_host_gets_the_repo_then_the_client(harness: Harness) -> None:
    harness.env["HOST_TYPE"] = "ubuntu"
    harness.apt.missing = {"proxmox-backup-client"}

    harness.run()

    keyring = f"{harness.installer.KEYRING_DIR}/proxmox-release-trixie.gpg"
    assert Path(keyring).read_bytes() == b"trixie keyring"
    source = Path(harness.installer.PBS_CLIENT_SOURCE)
    assert source.read_text(encoding="utf-8") == SOURCE.format(keyring=keyring)
    assert source.stat().st_mode & 0o777 == 0o644
    assert harness.apt.apt_calls[0][:2] == ["apt-get", "update"]
    assert harness.apt.apt_calls[1][:2] == ["apt-get", "install"]


def test_a_changed_source_refreshes_lists_fetched_before_it(harness: Harness) -> None:
    harness.ubuntu()
    harness.write(harness.installer.PBS_CLIENT_SOURCE, "Suites: bookworm\n")
    harness.apt.missing = {"proxmox-backup-client"}

    harness.run(lists_fresh=True)

    assert ["apt-get", "update", "-qq"] in harness.apt.apt_calls


def test_a_client_missing_after_install_fails(harness: Harness) -> None:
    harness.ubuntu()
    harness.apt = FakeApt(missing={"proxmox-backup-client"}, broken=True)
    with pytest.raises(InstallError, match="still missing after install"):
        harness.run()


def test_a_client_off_path_after_install_fails(harness: Harness) -> None:
    harness.ubuntu()
    harness.binaries.discard("proxmox-backup-client")
    with pytest.raises(InstallError, match="proxmox-backup-client not found after install"):
        harness.run()


# --------------------------------------------------------------------------
# credentials and keyfile
# --------------------------------------------------------------------------


def test_a_drifted_destination_is_restored_at_600(harness: Harness) -> None:
    harness.converge()
    harness.destination(0).write_text("tampered\n", encoding="utf-8")
    harness.destination(0).chmod(0o644)

    ctx = harness.run()

    assert harness.destination(0).read_text(encoding="utf-8") == "PBS_PASSWORD=secret-0\n"
    assert harness.destination(0).stat().st_mode & 0o777 == 0o600
    assert ctx.changes.names() == (str(harness.destination(0)),)
    assert harness.systemctl.actions() == []


def test_every_destination_is_installed(harness: Harness) -> None:
    harness.stage_destinations(2)
    harness.run()
    assert harness.destination(1).read_text(encoding="utf-8") == "PBS_PASSWORD=secret-1\n"


def test_encryption_installs_the_keyfile_at_600(harness: Harness) -> None:
    harness.env["ENCRYPT"] = "true"
    (harness.build_dir / "pbs-encryption.key").write_text("key", encoding="utf-8")

    harness.run()

    assert Path(harness.keyfile).read_text(encoding="utf-8") == "key"
    assert Path(harness.keyfile).stat().st_mode & 0o777 == 0o600


def test_purge_removes_a_keyfile_nothing_reads(harness: Harness) -> None:
    harness.write(harness.keyfile, "key")
    harness.env["PURGE_KEYFILE"] = "true"

    harness.run()

    assert not Path(harness.keyfile).exists()


def test_without_encrypt_or_purge_the_keyfile_is_left_alone(harness: Harness) -> None:
    """PVE hosts with encrypted vzdump storage read the same path."""
    harness.write(harness.keyfile, "key")
    harness.run()
    assert Path(harness.keyfile).read_text(encoding="utf-8") == "key"


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------


def test_a_changed_service_reloads_and_clears_its_failed_record_without_starting_it(
    harness: Harness,
) -> None:
    harness.converge()
    (harness.build_dir / SERVICE).write_text("# new service\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == ["daemon-reload", f"reset-failed {SERVICE}"]


def test_a_changed_timer_is_restarted(harness: Harness) -> None:
    harness.converge()
    (harness.build_dir / TIMER).write_text("# new timer\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == [
        "daemon-reload",
        f"reset-failed {SERVICE}",
        "daemon-reload",
        f"restart {TIMER}",
    ]


def test_paused_stops_the_timer_but_keeps_every_file(harness: Harness) -> None:
    harness.converge()
    harness.env["PAUSED"] = "true"

    harness.run()

    assert harness.systemctl.actions() == [f"disable --now {TIMER}"]
    assert harness.dest(TIMER).is_file()
    assert harness.dest(SERVICE).is_file()
    assert not any(call[1] == "list-timers" for call in harness.host_calls)


def test_force_rewrites_converged_files(harness: Harness) -> None:
    harness.converge()
    harness.force = True

    ctx = harness.run()

    assert ctx.changes.touched(TIMER, SERVICE)
