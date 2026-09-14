"""Remote installer for zfs-automation (freender/homelab-ops#30).

Every absolute destination is rebound under `tmp_path`, and the subprocess surfaces
-- `packages._run` (dpkg/apt), `systemd._run` (systemctl), and the installer's own
`_run` (zfs, useradd, chown, the known-hosts helper) and `_user_exists` -- are faked.
Nothing here touches `/etc`, a real unit, a pool, or apt.
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
INSTALLER_PATH = ROOT / "zfs-automation" / "scripts" / "install.py"

SNAP_TIMER = "homelab-zfs-snapshots.timer"
SCRUB_TIMER = "zfs-scrub.timer"
JOB = "homelab-zfs-replication-push-neo-data"
BASE_MAP = (
    ("sanoid.conf", "/etc/sanoid/sanoid.conf", "644"),
    ("homelab-zfs-snapshots.service", "/etc/systemd/system/homelab-zfs-snapshots.service", "644"),
    (SNAP_TIMER, f"/etc/systemd/system/{SNAP_TIMER}", "644"),
    ("homelab-zfs-snapshots.sh", "/usr/local/bin/homelab-zfs-snapshots", "755"),
    ("homelab-zfs-scrub.sh", "/usr/local/bin/homelab-zfs-scrub", "755"),
    ("zfs-scrub.service", "/etc/systemd/system/zfs-scrub.service", "644"),
    (SCRUB_TIMER, f"/etc/systemd/system/{SCRUB_TIMER}", "644"),
)


def job_map(name: str) -> tuple[tuple[str, str, str], ...]:
    return (
        (f"{name}.service", f"/etc/systemd/system/{name}.service", "644"),
        (f"{name}.timer", f"/etc/systemd/system/{name}.timer", "644"),
        (f"{name}.sh", f"/usr/local/bin/{name}", "755"),
    )


PUSH_MAP = (
    ("homelab-zfs-receive-only.sh", "/usr/local/sbin/homelab-zfs-receive-only", "755"),
    ("zfs-push-datasets.conf", "/etc/homelab/zfs-push-datasets.conf", "644"),
    ("zfs-push-authorized-keys", "/var/lib/homelab-zfs-push/.ssh/authorized_keys", "600"),
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
        if command[:2] == ["apt-get", "install"]:
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
        for name in ("SYSTEMD_DIR", "BIN_DIR", "ETC_HOMELAB", "PUSH_DATASETS_CONF", "RECEIVE_ONLY"):
            setattr(self.installer, name, self.under(getattr(self.installer, name)))
        Path(self.installer.SYSTEMD_DIR).mkdir(parents=True)
        Path(self.installer.BIN_DIR).mkdir(parents=True)

        self.build_dir = tmp_path / "remote" / "build" / "host"
        self.build_dir.mkdir(parents=True)
        self.file_map: dict[str, tuple[str, str]] = {}
        self.add_entries(BASE_MAP + job_map(JOB))

        self.push_home = self.under("/var/lib/homelab-zfs-push")
        self.state_dir = self.under("/var/lib/homelab")
        self.env = {
            "HOMELAB_STATE_DIR": self.state_dir,
            "PAUSED": "false",
            "PAUSED_REPLICATION_TIMERS": "",
            "ENABLE_ZFS_SNAPSHOTS": "true",
            "ENABLE_ZFS_REPLICATION": "true",
            "ZFS_REPLICATION_RECOVERY_START_FAILED": "false",
            "ENABLE_ZFS_SCRUB": "false",
            "ENABLE_ZFS_PUSH_TARGET": "false",
            "ZFS_PUSH_TARGET_USER": "zfs-push",
            "ZFS_PUSH_TARGET_HOME": self.push_home,
        }
        self.datasets: set[str] = {"tank", "tank/replica"}
        self.users: set[str] = set()
        self.apt = FakeApt()
        self.systemctl = FakeSystemctl(
            enabled={SNAP_TIMER, f"{JOB}.timer", "fstrim.timer"},
            active={SNAP_TIMER, f"{JOB}.timer", "fstrim.timer"},
        )
        self.host_calls: list[list[str]] = []
        self.helper_exit = 0
        self.force = False

    def under(self, dest: str) -> str:
        return str(self.sandbox / dest.lstrip("/"))

    def add_entries(self, entries) -> None:
        for name, dest, mode in entries:
            (self.build_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            self.file_map[name] = (self.under(dest), mode)

    def drop_job(self, name: str) -> None:
        for entry, _dest, _mode in job_map(name):
            del self.file_map[entry]

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def push_target(self, datasets: str = "tank/replica\n") -> None:
        self.env["ENABLE_ZFS_PUSH_TARGET"] = "true"
        self.add_entries(PUSH_MAP)
        (self.build_dir / "zfs-push-datasets.conf").write_text(datasets, encoding="utf-8")

    def _host(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.host_calls.append(list(command))
        if command[:2] == ["zfs", "list"]:
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.datasets else 1)
        if command[0] == "useradd":
            self.users.add(command[-1])
        if command[0].endswith("homelab-zfs-refresh-known-hosts"):
            return subprocess.CompletedProcess(command, self.helper_exit)
        return subprocess.CompletedProcess(command, 0)

    def run(self) -> InstallContext:
        self.monkeypatch.setattr(packages, "_run", self.apt)
        self.monkeypatch.setattr(packages, "_apt_updated", False)
        self.monkeypatch.setattr(systemd, "_run", self.systemctl)
        self.monkeypatch.setattr(self.installer, "_run", self._host)
        self.monkeypatch.setattr(self.installer, "_user_exists", lambda user: user in self.users)
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
        self.host_calls.clear()

    def written(self) -> list[Path]:
        return sorted(p for p in self.sandbox.rglob("*") if p.is_file())

    def mutating_host_calls(self) -> list[list[str]]:
        return [call for call in self.host_calls if call[:2] != ["zfs", "list"]]


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
    assert harness.host_calls == []


def test_a_converged_push_target_regrants_but_writes_nothing(harness: Harness) -> None:
    harness.push_target()
    harness.users.add("zfs-push")
    harness.converge()

    ctx = harness.run()

    assert not ctx.changes.any()
    assert harness.systemctl.actions() == []
    assert harness.mutating_host_calls() == [
        ["chown", "-R", "zfs-push:zfs-push", harness.push_home],
        ["zfs", "allow", "-u", "zfs-push", "create,mount,receive,hold,release", "tank/replica"],
    ]


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
        "daemon-reload",
        f"enable --now {JOB}.timer",
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
    assert harness.mutating_host_calls() == []


@pytest.mark.parametrize("key", load_installer().REQUIRED_ENV)
def test_a_missing_env_key_refuses(harness: Harness, key: str) -> None:
    del harness.env[key]
    _refuses(harness, f"missing: {key}")


def test_a_missing_paused_jobs_key_refuses(harness: Harness) -> None:
    del harness.env["PAUSED_REPLICATION_TIMERS"]
    _refuses(harness, "missing: PAUSED_REPLICATION_TIMERS")


@pytest.mark.parametrize("key", load_installer().FLAGS)
def test_a_flag_typo_refuses_instead_of_reading_as_false(harness: Harness, key: str) -> None:
    """The bash compared against "true", so a typo stopped the timer it guards."""
    harness.env[key] = "ture"
    _refuses(harness, f"{key} must be true or false")


def test_a_missing_staged_file_refuses(harness: Harness) -> None:
    (harness.build_dir / f"{JOB}.sh").unlink()
    _refuses(harness, f"missing staged files: {JOB}.sh")


def test_a_missing_base_entry_refuses(harness: Harness) -> None:
    del harness.file_map["sanoid.conf"]
    _refuses(harness, "missing file-map entries: sanoid.conf")


def test_a_push_target_without_its_entries_refuses(harness: Harness) -> None:
    harness.env["ENABLE_ZFS_PUSH_TARGET"] = "true"
    _refuses(harness, "missing file-map entries: homelab-zfs-receive-only.sh")


def test_a_push_dataset_with_no_existing_parent_refuses_before_the_user_exists(
    harness: Harness,
) -> None:
    """The bash created the user and installed the wrapper and keys first."""
    harness.push_target("elsewhere/replica\n")
    _refuses(harness, "parent not found: elsewhere/replica")
    assert harness.users == set()


def test_a_top_level_push_dataset_that_does_not_exist_refuses(harness: Harness) -> None:
    harness.push_target("nopool\n")
    _refuses(harness, "parent not found: nopool")


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def test_a_drifted_script_is_restored_without_touching_any_timer(harness: Harness) -> None:
    harness.converge()
    harness.dest(f"{JOB}.sh").write_text("tampered\n", encoding="utf-8")

    ctx = harness.run()

    assert harness.dest(f"{JOB}.sh").read_text(encoding="utf-8") == f"# {JOB}.sh\n"
    assert ctx.changes.names() == (f"{JOB}.sh",)
    assert harness.systemctl.actions() == []


def test_a_changed_service_reloads_but_restarts_no_timer(harness: Harness) -> None:
    """The bash restarted every enabled timer when any unit changed."""
    harness.converge()
    (harness.build_dir / f"{JOB}.service").write_text("# new\n", encoding="utf-8")

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
    harness.apt.missing = {"mbuffer"}

    harness.run()

    assert harness.apt.apt_calls[-1][:5] == ["apt-get", "install", "-y", "-q", "mbuffer"]


# --------------------------------------------------------------------------
# retired replication jobs
# --------------------------------------------------------------------------


def test_a_retired_job_is_stopped_and_every_file_removed(harness: Harness) -> None:
    old = "homelab-zfs-replication-old"
    harness.add_entries(job_map(old))
    harness.systemctl.enabled.add(f"{old}.timer")
    harness.converge()
    harness.drop_job(old)
    harness.systemctl.active.add(f"{old}.service")

    harness.run()

    for _name, dest, _mode in job_map(old):
        assert not Path(harness.under(dest)).exists()
    actions = harness.systemctl.actions()
    assert actions.index(f"disable --now {old}.timer") < actions.index(
        f"disable --now {old}.service"
    )
    assert f"reset-failed {old}.service" in actions
    assert actions[-1] == "daemon-reload"
    assert harness.dest(f"{JOB}.timer").is_file()


def test_retirement_leaves_mapped_jobs_and_unrelated_units_alone(harness: Harness) -> None:
    other = Path(harness.installer.SYSTEMD_DIR) / "homelab-zfs-snapshots.timer"
    harness.converge()

    harness.run()

    assert other.is_file()
    assert harness.systemctl.actions() == []


# --------------------------------------------------------------------------
# timer state
# --------------------------------------------------------------------------


def test_a_disabled_area_stops_its_timer(harness: Harness) -> None:
    harness.converge()
    harness.env["ENABLE_ZFS_REPLICATION"] = "false"

    harness.run()

    assert harness.systemctl.actions() == [f"disable --now {JOB}.timer"]


def test_an_enabled_timer_that_is_not_running_is_started(harness: Harness) -> None:
    harness.converge()
    harness.systemctl.active.discard(SNAP_TIMER)

    harness.run()

    assert harness.systemctl.actions() == [f"start {SNAP_TIMER}"]


def test_a_paused_job_is_stopped_while_the_others_run(harness: Harness) -> None:
    other = "homelab-zfs-replication-other"
    harness.add_entries(job_map(other))
    harness.systemctl.enabled.add(f"{other}.timer")
    harness.systemctl.active.add(f"{other}.timer")
    harness.converge()
    harness.env["PAUSED_REPLICATION_TIMERS"] = f"{JOB}.timer"

    harness.run()

    assert harness.systemctl.actions() == [f"disable --now {JOB}.timer"]
    assert harness.dest(f"{JOB}.timer").is_file()


def test_paused_freezes_every_timer_and_keeps_every_file(harness: Harness) -> None:
    harness.converge()
    harness.env["PAUSED"] = "true"
    harness.systemctl.enabled.add(SCRUB_TIMER)

    harness.run()

    assert harness.systemctl.actions() == [
        f"disable --now {SNAP_TIMER}",
        f"disable --now {SCRUB_TIMER}",
        f"disable --now {JOB}.timer",
    ]
    for name in harness.file_map:
        assert harness.dest(name).is_file()


def test_the_packaged_sanoid_timer_is_stopped(harness: Harness) -> None:
    harness.converge()
    harness.systemctl.enabled.add("sanoid.timer")

    harness.run()

    assert harness.systemctl.actions() == ["disable --now sanoid.timer"]


# --------------------------------------------------------------------------
# failed-replication recovery
# --------------------------------------------------------------------------


def test_recovery_restarts_a_failed_job(harness: Harness) -> None:
    harness.converge()
    harness.env["ZFS_REPLICATION_RECOVERY_START_FAILED"] = "true"
    harness.systemctl.failed.add(f"{JOB}.service")

    harness.run()

    assert harness.systemctl.actions() == [f"reset-failed {JOB}.service", f"start {JOB}.service"]


def test_recovery_skips_a_paused_job(harness: Harness) -> None:
    harness.converge()
    harness.env["ZFS_REPLICATION_RECOVERY_START_FAILED"] = "true"
    harness.env["PAUSED_REPLICATION_TIMERS"] = f"{JOB}.timer"
    harness.systemctl.failed.add(f"{JOB}.service")

    harness.run()

    assert f"start {JOB}.service" not in harness.systemctl.actions()


def test_recovery_is_off_when_replication_is(harness: Harness) -> None:
    harness.converge()
    harness.env["ZFS_REPLICATION_RECOVERY_START_FAILED"] = "true"
    harness.env["ENABLE_ZFS_REPLICATION"] = "false"
    harness.systemctl.failed.add(f"{JOB}.service")

    harness.run()

    assert f"start {JOB}.service" not in harness.systemctl.actions()


# --------------------------------------------------------------------------
# push target access
# --------------------------------------------------------------------------


def test_a_new_push_target_gets_its_user_files_and_grants(harness: Harness) -> None:
    harness.push_target("tank/replica\ntank/future/child\n")

    harness.run()

    assert harness.users == {"zfs-push"}
    assert Path(harness.push_home, ".ssh").stat().st_mode & 0o777 == 0o700
    assert harness.dest("zfs-push-authorized-keys").stat().st_mode & 0o777 == 0o600
    assert harness.dest("homelab-zfs-receive-only.sh").stat().st_mode & 0o777 == 0o755
    grants = [call for call in harness.host_calls if call[:2] == ["zfs", "allow"]]
    assert grants == [
        ["zfs", "allow", "-u", "zfs-push", "create,mount,receive,hold,release", "tank/replica"],
        ["zfs", "allow", "-d", "-u", "zfs-push", "create,mount,receive,hold,release", "tank"],
    ]


def test_a_host_that_stops_being_a_push_target_loses_its_access(harness: Harness) -> None:
    harness.push_target()
    harness.users.add("zfs-push")
    harness.converge()
    harness.env["ENABLE_ZFS_PUSH_TARGET"] = "false"
    for name, _dest, _mode in PUSH_MAP:
        del harness.file_map[name]

    harness.run()

    assert not Path(harness.installer.PUSH_DATASETS_CONF).exists()
    assert not Path(harness.installer.RECEIVE_ONLY).exists()
    assert not Path(harness.push_home, ".ssh", "authorized_keys").exists()
    assert not any(call[:2] == ["zfs", "allow"] for call in harness.host_calls)


# --------------------------------------------------------------------------
# known-hosts refresh
# --------------------------------------------------------------------------


def test_the_known_hosts_helper_runs_when_mapped(harness: Harness) -> None:
    harness.add_entries(
        (
            (
                "homelab-zfs-refresh-known-hosts.sh",
                "/usr/local/sbin/homelab-zfs-refresh-known-hosts",
                "755",
            ),
        )
    )

    harness.run()

    assert [harness.under("/usr/local/sbin/homelab-zfs-refresh-known-hosts")] in harness.host_calls


def test_a_failing_known_hosts_helper_fails_the_deploy(harness: Harness) -> None:
    harness.add_entries(
        (
            (
                "homelab-zfs-refresh-known-hosts.sh",
                "/usr/local/sbin/homelab-zfs-refresh-known-hosts",
                "755",
            ),
        )
    )
    harness.helper_exit = 3

    with pytest.raises(InstallError, match="failed \\(exit 3\\)"):
        harness.run()
