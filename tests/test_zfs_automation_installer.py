"""Remote installer for zfs-automation (freender/homelab-ops#30).

Every absolute destination is rebound under `tmp_path`, and the subprocess surfaces
-- `packages._run` (dpkg/apt) and `systemd._run` (systemctl) -- are faked. Nothing
here touches `/etc`, a real unit, a pool, or apt.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.packages import APT_GET

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "zfs-automation" / "scripts" / "install.py"

SNAP_TIMER = "homelab-zfs-snapshots.timer"
SCRUB_TIMER = "zfs-scrub.timer"
SNAP_SCRIPT = "homelab-zfs-snapshots.sh"
SNAP_SERVICE = "homelab-zfs-snapshots.service"
BASE_MAP = (
    ("sanoid.conf", "/etc/sanoid/sanoid.conf", "644"),
    ("homelab-zfs-snapshots.service", "/etc/systemd/system/homelab-zfs-snapshots.service", "644"),
    (SNAP_TIMER, f"/etc/systemd/system/{SNAP_TIMER}", "644"),
    ("homelab-zfs-snapshots.sh", "/usr/local/bin/homelab-zfs-snapshots", "755"),
    ("homelab-zfs-scrub.sh", "/usr/local/bin/homelab-zfs-scrub", "755"),
    ("zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service", "644"),
    (SCRUB_TIMER, f"/etc/systemd/system/{SCRUB_TIMER}", "644"),
)


def load_installer():
    """Fresh load per test: these rebind the module's destination constants."""
    spec = importlib.util.spec_from_file_location("zfs_automation_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApt:
    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = set(missing or ())
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "dpkg-query":
            ok = command[-1] not in self.missing
            return subprocess.CompletedProcess(
                command, 0 if ok else 1, stdout="install ok installed"
            )
        if command[:4] == [*APT_GET, "install"]:
            self.missing -= set(command[4:])
        return subprocess.CompletedProcess(command, 0)

    @property
    def apt_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "apt-get"]


class FakeSystemctl:
    def __init__(self, enabled: set[str], active: set[str], failed: set[str] | None = None) -> None:
        self.enabled = enabled
        self.active = active
        self.failed = set(failed or ())
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb, unit = command[1], command[-1]
        states = {"is-enabled": self.enabled, "is-active": self.active, "is-failed": self.failed}
        if verb in states:
            return subprocess.CompletedProcess(command, 0 if unit in states[verb] else 1)
        if verb == "start":
            self.failed.discard(unit)
        return subprocess.CompletedProcess(command, 0)

    def actions(self) -> list[str]:
        queries = {"is-enabled", "is-active", "is-failed"}
        return [" ".join(call[1:]) for call in self.calls if call[1] not in queries]


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.sandbox = tmp_path / "hostfs"
        self.installer = load_installer()

        self.build_dir = tmp_path / "remote" / "build" / "host"
        self.build_dir.mkdir(parents=True)
        self.file_map: dict[str, tuple[str, str]] = {}
        self.add_entries(BASE_MAP)

        self.state_dir = self.under("/var/lib/homelab")
        self.env = {
            "HOMELAB_STATE_DIR": self.state_dir,
            "PAUSED": "false",
            "ENABLE_ZFS_SNAPSHOTS": "true",
            "ENABLE_ZFS_SCRUB": "false",
        }
        self.apt = FakeApt()
        self.systemctl = FakeSystemctl(
            enabled={SNAP_TIMER, "fstrim.timer"},
            active={SNAP_TIMER, "fstrim.timer"},
        )
        self.force = False

    def under(self, dest: str) -> str:
        return str(self.sandbox / dest.lstrip("/"))

    def add_entries(self, entries) -> None:
        for name, dest, mode in entries:
            (self.build_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            self.file_map[name] = (self.under(dest), mode)

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def run(self) -> InstallContext:
        self.monkeypatch.setattr(packages, "_run", self.apt)
        self.monkeypatch.setattr(packages, "_apt_updated", False)
        self.monkeypatch.setattr(systemd, "_run", self.systemctl)
        ctx = InstallContext(
            host="host",
            script_dir=self.build_dir.parent.parent,
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

    def written(self) -> list[Path]:
        return sorted(p for p in self.sandbox.rglob("*") if p.is_file())


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# converged hosts
# --------------------------------------------------------------------------


def test_a_converged_redeploy_changes_nothing(harness: Harness) -> None:
    harness.converge()
    mtimes = {path: path.stat().st_mtime_ns for path in harness.written()}

    ctx = harness.run()

    assert not ctx.changes.any()
    assert {path: path.stat().st_mtime_ns for path in harness.written()} == mtimes
    assert harness.apt.apt_calls == []
    assert harness.systemctl.actions() == []


def test_first_install_writes_every_file_and_enables_the_timers(harness: Harness) -> None:
    harness.systemctl.enabled.clear()
    harness.systemctl.active.clear()

    harness.run()

    for name, (_dest, mode) in harness.file_map.items():
        assert harness.dest(name).read_text(encoding="utf-8") == f"# {name}\n"
        assert oct(harness.dest(name).stat().st_mode & 0o777)[2:] == mode
    assert harness.systemctl.actions() == [
        "enable --now fstrim.timer",
        "daemon-reload",
        "daemon-reload",
        f"enable --now {SNAP_TIMER}",
    ]


# --------------------------------------------------------------------------
# refusals: every one of them before any write
# --------------------------------------------------------------------------


def _refuses(harness: Harness, match: str) -> None:
    with pytest.raises(InstallError, match=match):
        harness.run()
    assert harness.written() == []
    assert harness.systemctl.calls == []
    assert harness.apt.calls == []


@pytest.mark.parametrize("key", load_installer().REQUIRED_ENV)
def test_a_missing_env_key_refuses(harness: Harness, key: str) -> None:
    del harness.env[key]
    _refuses(harness, f"missing: {key}")


@pytest.mark.parametrize("key", load_installer().FLAGS)
def test_a_flag_typo_refuses_instead_of_reading_as_false(harness: Harness, key: str) -> None:
    """The bash compared against "true", so a typo stopped the timer it guards."""
    harness.env[key] = "ture"
    _refuses(harness, f"{key} must be true or false")


def test_a_missing_staged_file_refuses(harness: Harness) -> None:
    (harness.build_dir / SNAP_SCRIPT).unlink()
    _refuses(harness, f"missing staged files: {SNAP_SCRIPT}")


def test_a_missing_base_entry_refuses(harness: Harness) -> None:
    del harness.file_map["sanoid.conf"]
    _refuses(harness, "missing file-map entries: sanoid.conf")


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def test_a_drifted_script_is_restored_without_touching_any_timer(harness: Harness) -> None:
    harness.converge()
    harness.dest(SNAP_SCRIPT).write_text("tampered\n", encoding="utf-8")

    ctx = harness.run()

    assert harness.dest(SNAP_SCRIPT).read_text(encoding="utf-8") == f"# {SNAP_SCRIPT}\n"
    assert ctx.changes.names() == (SNAP_SCRIPT,)
    assert harness.systemctl.actions() == []


def test_a_changed_service_reloads_but_restarts_no_timer(harness: Harness) -> None:
    """The bash restarted every enabled timer when any unit changed."""
    harness.converge()
    (harness.build_dir / SNAP_SERVICE).write_text("# new\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == ["daemon-reload"]


def test_a_changed_timer_restarts_only_that_timer(harness: Harness) -> None:
    harness.converge()
    (harness.build_dir / SNAP_TIMER).write_text("# new\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == [
        "daemon-reload",
        "daemon-reload",
        f"restart {SNAP_TIMER}",
    ]


def test_the_shadow_copy_is_removed(harness: Harness) -> None:
    managed = Path(harness.state_dir) / "zfs-automation-managed"
    managed.mkdir(parents=True)
    (managed / "sanoid.conf").write_text("copy", encoding="utf-8")

    harness.run()

    assert not managed.exists()
    assert Path(harness.state_dir).is_dir()


def test_force_rewrites_converged_files(harness: Harness) -> None:
    harness.converge()
    harness.force = True

    ctx = harness.run()

    assert ctx.changes.touched("sanoid.conf", SNAP_TIMER)


def test_missing_packages_are_installed(harness: Harness) -> None:
    harness.apt.missing = {"sanoid"}

    harness.run()

    assert harness.apt.apt_calls[-1][:7] == [*APT_GET, "install", "-y", "-q", "sanoid"]


# --------------------------------------------------------------------------
# timer state
# --------------------------------------------------------------------------


def test_a_disabled_area_stops_its_timer(harness: Harness) -> None:
    harness.converge()
    harness.env["ENABLE_ZFS_SNAPSHOTS"] = "false"

    harness.run()

    assert harness.systemctl.actions() == [f"disable --now {SNAP_TIMER}"]


def test_an_enabled_timer_that_is_not_running_is_started(harness: Harness) -> None:
    harness.converge()
    harness.systemctl.active.discard(SNAP_TIMER)

    harness.run()

    assert harness.systemctl.actions() == [f"start {SNAP_TIMER}"]


def test_paused_freezes_every_timer_and_keeps_every_file(harness: Harness) -> None:
    harness.converge()
    harness.env["PAUSED"] = "true"
    harness.systemctl.enabled.add(SCRUB_TIMER)

    harness.run()

    assert harness.systemctl.actions() == [
        f"disable --now {SNAP_TIMER}",
        f"disable --now {SCRUB_TIMER}",
    ]
    for name in harness.file_map:
        assert harness.dest(name).is_file()


def test_the_packaged_sanoid_timer_is_stopped(harness: Harness) -> None:
    harness.converge()
    harness.systemctl.enabled.add("sanoid.timer")

    harness.run()

    assert harness.systemctl.actions() == ["disable --now sanoid.timer"]
