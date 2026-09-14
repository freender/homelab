"""docker: orchestrator file map and remote installer (freender/homelab-ops#30).

Every destination is rebound under `tmp_path` and systemctl is faked. Nothing
here touches `/mnt/cache/appdata`, `/etc/systemd`, or a real unit.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import docker
from homelab_install import systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.main import _parse_env_file, _parse_file_map

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "docker" / "scripts" / "install.py"
SERVICE = docker.UPDATE_SERVICE
TIMER = docker.UPDATE_TIMER


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def render(tmp_path: Path, schedule: str) -> tuple[Path, tuple]:
    for sub in ("templates", "scripts"):
        link = tmp_path / "docker" / sub
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(REPO_ROOT / "docker" / sub)
    return docker.render_build(tmp_path, "neo", schedule)


@pytest.mark.parametrize(("schedule", "has_units"), [("*-*-* 23:30:00", True), ("", False)])
def test_file_map_matches_what_was_rendered(tmp_path: Path, schedule: str, has_units: bool) -> None:
    """The map the installer reads and the files actually rendered must agree,
    or `files.install` raises on a missing source."""
    build_dir, specs = render(tmp_path, schedule)

    file_map = _parse_file_map(build_dir / "file-map.conf")
    rendered = {p.name for p in build_dir.iterdir()} - {"file-map.conf"}
    assert set(file_map) == rendered == {spec.build_name for spec in specs}
    assert (SERVICE in file_map) is has_units
    assert (TIMER in file_map) is has_units


def test_helper_scripts_are_pinned_executable_and_the_env_is_not(tmp_path: Path) -> None:
    build_dir, _ = render(tmp_path, "*-*-* 23:30:00")
    file_map = _parse_file_map(build_dir / "file-map.conf")

    assert file_map["start.sh"] == ("/mnt/cache/appdata/start.sh", "755")
    assert file_map["docker-common.sh"] == (
        "/mnt/cache/appdata/.homelab/docker/docker-common.sh",
        "755",
    )
    assert file_map["env"] == ("/mnt/cache/appdata/.homelab/docker/env", "644")
    assert file_map[SERVICE] == (f"/etc/systemd/system/{SERVICE}", "644")


def test_the_update_service_runs_the_start_script_the_map_installs(tmp_path: Path) -> None:
    """If these drifted, the timer would fire a script the deploy never updates."""
    build_dir, _ = render(tmp_path, "*-*-* 23:30:00")
    file_map = _parse_file_map(build_dir / "file-map.conf")

    service = (build_dir / SERVICE).read_text(encoding="utf-8")
    assert f"ExecStart={file_map['start.sh'][0]}\n" in service


def test_helper_scripts_are_copied_verbatim(tmp_path: Path) -> None:
    build_dir, _ = render(tmp_path, "")
    for name in docker.HELPER_SCRIPTS:
        source = REPO_ROOT / "docker" / "scripts" / name
        assert (build_dir / name).read_bytes() == source.read_bytes()


@pytest.mark.parametrize(("schedule", "flag"), [("*-*-* 02:00:00", "true"), ("", "false")])
def test_the_env_flag_follows_the_schedule(tmp_path: Path, schedule: str, flag: str) -> None:
    build_dir, _ = render(tmp_path, schedule)
    assert _parse_env_file(build_dir / "env")["ENABLE_DOCKER_UPDATE_TIMER"] == flag


def test_the_timer_is_rendered_with_the_schedule(tmp_path: Path) -> None:
    build_dir, _ = render(tmp_path, "*-*-* 23:30:00")
    assert "OnCalendar=*-*-* 23:30:00" in (build_dir / TIMER).read_text(encoding="utf-8")


def test_validate_requires_every_helper_script(tmp_path: Path) -> None:
    (tmp_path / "docker" / "templates").mkdir(parents=True)
    for name in docker.TEMPLATE_FILES:
        (tmp_path / "docker" / "templates" / name).write_text("", encoding="utf-8")
    (tmp_path / "docker" / "scripts").mkdir()

    with pytest.raises(ValueError, match="start.sh"):
        docker.validate(tmp_path, [])


def test_orchestrator_stages_the_python_installer() -> None:
    assert docker.INSTALLER == "scripts/install.py"
    assert docker.INTERPRETER == "python3"


def test_file_names_agree_across_the_wire() -> None:
    installer = load_installer()
    assert installer.UPDATE_SERVICE == docker.UPDATE_SERVICE
    assert installer.UPDATE_TIMER == docker.UPDATE_TIMER
    assert set(installer.HELPER_FILES) == {spec.build_name for spec in docker.HELPER_SPECS}


# ---------------------------------------------------------------------------
# Remote installer
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("docker_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSystemctl:
    def __init__(self) -> None:
        self.enabled = {TIMER}
        self.active = {TIMER}
        self.failed: set[str] = set()
        self.fail_on_start: set[str] = set()
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb, unit = command[1], command[-1]
        code = 0
        if verb == "is-enabled":
            code = 0 if unit in self.enabled else 1
        elif verb == "is-active":
            code = 0 if unit in self.active else 3
        elif verb == "is-failed":
            code = 0 if unit in self.failed else 1
        elif verb == "reset-failed":
            self.failed.discard(unit)
        elif verb == "start" and unit in self.fail_on_start:
            self.failed.add(unit)
            code = 1
        elif verb == "disable":
            self.enabled.discard(unit)
            self.active.discard(unit)
        return subprocess.CompletedProcess(command, code, "", "")

    def actions(self) -> list[str]:
        queries = {"is-enabled", "is-active", "is-failed", "reset-failed"}
        return [" ".join(call[1:]) for call in self.calls if call[1] not in queries]


class Host:
    """A sandboxed host filesystem plus a staged bundle."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timer: bool) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "hostfs"
        self.systemctl = FakeSystemctl()
        monkeypatch.setattr(systemd, "_run", self.systemctl)

        self.build_dir = tmp_path / "remote" / "build" / "h"
        self.build_dir.mkdir(parents=True)
        specs = docker.file_specs(timer)
        for spec in specs:
            source = self.build_dir / spec.build_name
            source.write_text(f"# {spec.build_name}\n", encoding="utf-8")
        self.file_map = {
            spec.build_name: (str(self.root / spec.remote_path.lstrip("/")), spec.mode)
            for spec in specs
        }
        self.env = {"ENABLE_DOCKER_UPDATE_TIMER": "true" if timer else "false"}

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def ctx(self) -> InstallContext:
        return InstallContext(
            host="h",
            script_dir=self.build_dir.parents[1],
            build_dir=self.build_dir,
            env=self.env,
            deploy_env={},
            file_map=self.file_map,
            force_update=False,
        )

    def deploy(self) -> None:
        self.systemctl.calls.clear()
        self.installer.install(self.ctx())


@pytest.fixture
def host_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return lambda timer=True: Host(tmp_path, monkeypatch, timer)


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_a_fresh_host_gets_every_file_at_its_mode(host_for) -> None:
    host = host_for()
    host.systemctl.enabled = set()
    host.systemctl.active = set()
    host.deploy()

    for name in ("start.sh", "rm.sh", "rebuild.sh", "docker-common.sh"):
        assert mode_of(host.dest(name)) == 0o755
    assert mode_of(host.dest("env")) == 0o644
    assert mode_of(host.dest(SERVICE)) == 0o644
    assert f"enable --now {TIMER}" in host.systemctl.actions()


def test_a_group_writable_start_script_is_tightened_without_a_rewrite(host_for) -> None:
    """The bash only `chmod +x`ed, so live copies had drifted to 775/777 while
    running as root from the update service."""
    host = host_for()
    host.deploy()
    start = host.dest("start.sh")
    start.chmod(0o777)
    mtime = start.stat().st_mtime_ns

    host.deploy()

    assert mode_of(start) == 0o755
    assert start.stat().st_mtime_ns == mtime


def test_an_unchanged_redeploy_touches_no_unit(host_for) -> None:
    host = host_for()
    host.deploy()
    host.deploy()

    assert host.systemctl.actions() == []


@pytest.mark.parametrize("unit", [SERVICE, TIMER])
def test_a_changed_unit_reloads_and_restarts_the_timer(host_for, unit: str) -> None:
    host = host_for()
    host.deploy()
    (host.build_dir / unit).write_text("# new\n", encoding="utf-8")
    host.deploy()

    assert host.systemctl.actions() == ["daemon-reload", f"restart {TIMER}"]


def test_a_changed_script_does_not_restart_the_timer(host_for) -> None:
    """start.sh is exec'd afresh on every fire; nothing caches it."""
    host = host_for()
    host.deploy()
    (host.build_dir / "start.sh").write_text("# new\n", encoding="utf-8")
    host.deploy()

    assert host.systemctl.actions() == []
    assert host.dest("start.sh").read_text(encoding="utf-8") == "# new\n"


def test_an_enabled_but_stopped_timer_is_started(host_for) -> None:
    host = host_for()
    host.deploy()
    host.systemctl.active = set()
    host.deploy()

    assert host.systemctl.actions() == [f"start {TIMER}"]


def test_no_schedule_stops_the_timer_and_leaves_its_files(host_for) -> None:
    host = host_for(timer=False)
    unit_file = host.root / "etc" / "systemd" / "system" / TIMER
    unit_file.parent.mkdir(parents=True)
    unit_file.write_text("[Timer]\n", encoding="utf-8")

    host.deploy()

    assert host.systemctl.actions() == [f"disable --now {TIMER}"]
    assert unit_file.exists()


def test_no_schedule_on_a_host_without_the_timer_does_nothing(host_for) -> None:
    host = host_for(timer=False)
    host.systemctl.enabled = set()
    host.systemctl.active = set()
    host.deploy()

    assert host.systemctl.actions() == []
    assert not any(SERVICE in " ".join(call) for call in host.systemctl.calls)


def test_a_failed_update_run_is_retried_on_redeploy(host_for) -> None:
    host = host_for()
    host.deploy()
    host.systemctl.failed = {SERVICE}
    host.deploy()

    assert ["systemctl", "reset-failed", SERVICE] in host.systemctl.calls
    assert f"start {SERVICE}" in host.systemctl.actions()


def test_a_healthy_update_service_is_never_started(host_for) -> None:
    """Starting it off-schedule pulls every image; only a failed one earns that."""
    host = host_for()
    host.deploy()

    assert not any(call[1:3] == ["start", SERVICE] for call in host.systemctl.calls)


def test_a_still_failing_update_run_does_not_fail_the_deploy(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.systemctl.failed = {SERVICE}
    host.systemctl.fail_on_start = {SERVICE}
    host.deploy()

    assert "still failing after restart" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("env", "match"),
    [({}, "missing: ENABLE_DOCKER_UPDATE_TIMER"), ({"ENABLE_DOCKER_UPDATE_TIMER": "ture"}, "ture")],
)
def test_a_bad_flag_fails_before_anything_is_touched(
    host_for, env: dict[str, str], match: str
) -> None:
    """An absent or typo'd flag must not read as "turn the update timer off"."""
    host = host_for()
    host.env = env

    with pytest.raises(InstallError, match=match):
        host.deploy()

    assert host.systemctl.calls == []
    assert not host.root.exists()


def test_a_timer_host_whose_map_lacks_the_units_fails(host_for) -> None:
    """Flag and map disagreeing is a broken render, not a host without a timer."""
    host = host_for(timer=False)
    host.env = {"ENABLE_DOCKER_UPDATE_TIMER": "true"}

    with pytest.raises(InstallError, match="missing file-map entry"):
        host.deploy()
