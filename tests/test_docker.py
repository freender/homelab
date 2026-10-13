"""docker: orchestrator file map and remote installer (freender/homelab-ops#30, #151).

Every destination is rebound under `tmp_path`, and systemctl, apt, dpkg, snap and
usermod are faked. Nothing here touches `/mnt/cache/appdata`, `/etc`, or a real
unit or package.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from homelab.modules import docker
from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.main import _parse_env_file, _parse_file_map

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "docker" / "scripts" / "install.py"
SERVICE = docker.UPDATE_SERVICE
TIMER = docker.UPDATE_TIMER
ENGINE_UNITS = ("containerd.service", "docker.service")
SOURCE = "deb [arch=amd64 signed-by={key}] https://download.docker.com/linux/debian trixie stable\n"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def render(tmp_path: Path, schedule: str, user: str = "freender") -> tuple[Path, tuple]:
    for sub in ("templates", "scripts", "configs"):
        link = tmp_path / "docker" / sub
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(REPO_ROOT / "docker" / sub)
    return docker.render_build(tmp_path, "neo", schedule, user)


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


def test_the_group_user_reaches_the_env(tmp_path: Path) -> None:
    build_dir, _ = render(tmp_path, "", user="sysadm")
    assert _parse_env_file(build_dir / "env")["DOCKER_GROUP_USER"] == "sysadm"


def test_engine_configs_are_copied_verbatim_to_their_system_paths(tmp_path: Path) -> None:
    build_dir, _ = render(tmp_path, "")
    file_map = _parse_file_map(build_dir / "file-map.conf")

    for name in docker.ENGINE_CONFIGS:
        source = REPO_ROOT / "docker" / "configs" / name
        assert (build_dir / name).read_bytes() == source.read_bytes()
        assert file_map[name][1] == "644"
    assert file_map["docker.asc"][0] == "/etc/apt/keyrings/docker.asc"
    assert file_map["daemon.json"][0] == "/etc/docker/daemon.json"
    assert file_map["docker-timeout.conf"][0].startswith("/etc/systemd/system/docker.service.d/")
    assert file_map["containerd-override.conf"][0].startswith(
        "/etc/systemd/system/containerd.service.d/"
    )


class FakeRegistry:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, host: str, key: str, default=None):
        return self.values.get(key, default)


def test_the_group_user_is_the_interactive_ssh_user() -> None:
    registry = FakeRegistry({"config.user": "root", "config.ssh_config.user": "denysc"})
    assert docker.group_user(registry, "cottonwood") == "denysc"


def test_the_group_user_falls_back_to_the_deploy_user() -> None:
    assert docker.group_user(FakeRegistry({"config.user": "root"}), "h") == "root"


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


def test_validate_requires_every_engine_config(tmp_path: Path) -> None:
    for sub in ("templates", "scripts"):
        (tmp_path / "docker" / sub).mkdir(parents=True)
    for name in docker.TEMPLATE_FILES:
        (tmp_path / "docker" / "templates" / name).write_text("", encoding="utf-8")
    for name in docker.HELPER_SCRIPTS:
        (tmp_path / "docker" / "scripts" / name).write_text("", encoding="utf-8")
    (tmp_path / "docker" / "configs").mkdir()

    with pytest.raises(ValueError, match="docker.asc"):
        docker.validate(tmp_path, [])


def test_the_shipped_key_is_dockers_release_key() -> None:
    """Pinned so a swapped key is a test failure, not a silent trust change."""
    import hashlib

    key = (REPO_ROOT / "docker" / "configs" / "docker.asc").read_bytes()
    assert hashlib.sha256(key).hexdigest() == (
        "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"
    )


def test_orchestrator_stages_the_python_installer() -> None:
    assert docker.INSTALLER == "scripts/install.py"
    assert docker.INTERPRETER == "python3"


def test_file_names_agree_across_the_wire() -> None:
    installer = load_installer()
    assert installer.UPDATE_SERVICE == docker.UPDATE_SERVICE
    assert installer.UPDATE_TIMER == docker.UPDATE_TIMER
    assert set(installer.HELPER_FILES) == {spec.build_name for spec in docker.HELPER_SPECS}
    engine = {installer.KEYRING_FILE, installer.DAEMON_CONFIG, *installer.DROP_INS}
    assert engine == set(docker.ENGINE_CONFIGS)


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
        self.enabled = {TIMER, *ENGINE_UNITS}
        self.active = {TIMER, *ENGINE_UNITS}
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


class FakeRun:
    """apt, dpkg, snap and usermod: answers by longest argv prefix, records calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], tuple[int, str]] = {}
        self.installed = set(packages_installed())
        self.before_install = lambda: None

    def answer(self, *argv: str, code: int = 0, stdout: str = "") -> None:
        self.answers[argv] = (code, stdout)

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["dpkg-query", "-W"]:
            status = "install ok installed" if argv[-1] in self.installed else ""
            return subprocess.CompletedProcess(argv, 0 if status else 1, stdout=status)
        if "install" in argv and argv[0] == "apt-get":
            self.before_install()
            self.installed.update(argv[argv.index("-q") + 1 :])
        for length in range(len(argv), 0, -1):
            if tuple(argv[:length]) in self.answers:
                code, stdout = self.answers[tuple(argv[:length])]
                break
        else:
            code, stdout = 0, ""
        return subprocess.CompletedProcess(argv, code, stdout=stdout)

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


def packages_installed() -> tuple[str, ...]:
    return load_installer().ENGINE_PACKAGES


class Host:
    """A sandboxed host filesystem plus a staged bundle: Debian trixie with the
    engine packages already installed, `freender` in the docker group."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timer: bool) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "hostfs"
        self.systemctl = FakeSystemctl()
        monkeypatch.setattr(systemd, "_run", self.systemctl)
        monkeypatch.setattr(packages, "_apt_updated", False)

        self.run = FakeRun()
        self.run.answer("dpkg", "--print-architecture", stdout="amd64\n")
        monkeypatch.setattr(packages, "_run", self.run)
        monkeypatch.setattr(self.installer, "_run", self.run)
        self.binaries: set[str] = set()
        monkeypatch.setattr(
            self.installer,
            "_which",
            lambda name: f"/usr/bin/{name}" if name in self.binaries else None,
        )
        self.users = {"freender": SimpleNamespace(pw_gid=1000), "root": SimpleNamespace(pw_gid=0)}
        self.docker_group: SimpleNamespace | None = SimpleNamespace(gr_mem=["freender"], gr_gid=991)
        monkeypatch.setattr(self.installer, "pwd", SimpleNamespace(getpwnam=self._getpwnam))
        monkeypatch.setattr(self.installer, "grp", SimpleNamespace(getgrnam=self._getgrnam))

        self.os_release = self.root / "etc" / "os-release"
        self.os_release.parent.mkdir(parents=True)
        self.os_release.write_text("ID=debian\nVERSION_CODENAME=trixie\n", encoding="utf-8")
        self.docker_list = self.root / "etc/apt/sources.list.d/docker.list"
        self.docker_sources = self.root / "etc/apt/sources.list.d/docker.sources"
        self.backup_dir = self.root / "var/backups/homelab/docker"
        monkeypatch.setattr(self.installer, "OS_RELEASE", str(self.os_release))
        monkeypatch.setattr(self.installer, "DOCKER_LIST", str(self.docker_list))
        monkeypatch.setattr(self.installer, "STALE_SOURCES", (str(self.docker_sources),))
        monkeypatch.setattr(self.installer, "BACKUP_DIR", str(self.backup_dir))

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
        self.env = {
            "ENABLE_DOCKER_UPDATE_TIMER": "true" if timer else "false",
            "DOCKER_GROUP_USER": "freender",
        }
        # The engine is already converged; the appdata helpers are not.
        for name in docker.ENGINE_CONFIGS:
            self.dest(name).parent.mkdir(parents=True, exist_ok=True)
            self.dest(name).write_bytes((self.build_dir / name).read_bytes())
        self.docker_list.parent.mkdir(parents=True, exist_ok=True)
        self.docker_list.write_text(self.source(), encoding="utf-8")

    def _getpwnam(self, name: str):
        if name not in self.users:
            raise KeyError(name)
        return self.users[name]

    def _getgrnam(self, name: str):
        if name != "docker" or self.docker_group is None:
            raise KeyError(name)
        return self.docker_group

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def snapshot(self) -> dict[str, tuple[int, bytes]]:
        return {
            str(path): (path.stat().st_mtime_ns, path.read_bytes())
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def source(self) -> str:
        return SOURCE.format(key=self.dest("docker.asc"))

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
        self.run.calls.clear()
        monkey_reset_apt()
        self.installer.install(self.ctx())


def monkey_reset_apt() -> None:
    """`_apt_updated` is per installer process; each deploy here is one."""
    packages._apt_updated = False


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
    host.systemctl.active.discard(TIMER)
    host.deploy()

    assert host.systemctl.actions() == [f"start {TIMER}"]


def test_no_schedule_stops_the_timer_and_leaves_its_files(host_for) -> None:
    host = host_for(timer=False)
    unit_file = host.root / "etc" / "systemd" / "system" / TIMER
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    unit_file.write_text("[Timer]\n", encoding="utf-8")

    host.deploy()

    assert host.systemctl.actions() == [f"disable --now {TIMER}"]
    assert unit_file.exists()


def test_no_schedule_on_a_host_without_the_timer_does_nothing(host_for) -> None:
    host = host_for(timer=False)
    host.systemctl.enabled.discard(TIMER)
    host.systemctl.active.discard(TIMER)
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
    [
        ({}, "missing: ENABLE_DOCKER_UPDATE_TIMER"),
        ({"ENABLE_DOCKER_UPDATE_TIMER": "ture", "DOCKER_GROUP_USER": "freender"}, "ture"),
        ({"ENABLE_DOCKER_UPDATE_TIMER": "true"}, "missing: DOCKER_GROUP_USER"),
        ({"ENABLE_DOCKER_UPDATE_TIMER": "true", "DOCKER_GROUP_USER": "nobody2"}, "nobody2"),
    ],
)
def test_a_bad_flag_fails_before_anything_is_touched(
    host_for, env: dict[str, str], match: str
) -> None:
    """An absent or typo'd flag must not read as "turn the update timer off"."""
    host = host_for()
    host.env = env
    before = host.snapshot()

    with pytest.raises(InstallError, match=match):
        host.deploy()

    assert host.systemctl.calls == []
    assert host.run.calls == []
    assert host.snapshot() == before


def test_a_timer_host_whose_map_lacks_the_units_fails(host_for) -> None:
    """Flag and map disagreeing is a broken render, not a host without a timer."""
    host = host_for(timer=False)
    host.env["ENABLE_DOCKER_UPDATE_TIMER"] = "true"

    with pytest.raises(InstallError, match="missing file-map entry"):
        host.deploy()


# ---------------------------------------------------------------------------
# Engine (#151)
# ---------------------------------------------------------------------------


def test_a_converged_engine_is_not_touched(host_for, capsys: pytest.CaptureFixture[str]) -> None:
    host = host_for()
    host.deploy()
    before = host.snapshot()

    host.deploy()

    assert host.snapshot() == before
    assert host.systemctl.actions() == []
    for mutating in (("apt-get",), ("usermod",), ("snap",)):
        assert not host.run.ran(*mutating), mutating
    assert "restart containerd and docker" not in capsys.readouterr().out


def test_the_source_names_the_mapped_key_and_the_host_release(host_for) -> None:
    host = host_for()
    host.docker_list.unlink()
    host.os_release.write_text('ID=ubuntu\nVERSION_CODENAME="resolute"\n', encoding="utf-8")
    host.run.answer("dpkg", "--print-architecture", stdout="arm64\n")

    host.deploy()

    assert host.docker_list.read_text(encoding="utf-8") == (
        f"deb [arch=arm64 signed-by={host.dest('docker.asc')}] "
        "https://download.docker.com/linux/ubuntu resolute stable\n"
    )


def test_a_fresh_host_gets_config_before_packages_and_needs_no_restart(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    for name in docker.ENGINE_CONFIGS:
        host.dest(name).unlink()
    host.docker_list.unlink()
    host.run.installed = set()
    host.systemctl.enabled -= set(ENGINE_UNITS)
    host.systemctl.active -= set(ENGINE_UNITS)
    seen_at_install: list[bool] = []
    host.run.before_install = lambda: seen_at_install.append(
        all(host.dest(name).is_file() for name in docker.ENGINE_CONFIGS)
        and host.docker_list.is_file()
    )

    host.deploy()

    assert seen_at_install == [True]
    assert host.run.ran(*packages.APT_GET, "update")
    install = next(c for c in host.run.calls if c[:1] == ["apt-get"] and "install" in c)
    assert install[install.index("-q") + 1 :] == list(host.installer.ENGINE_PACKAGES)
    actions = host.systemctl.actions()
    assert "daemon-reload" in actions
    assert [f"enable --now {unit}" for unit in ENGINE_UNITS] == [
        a for a in actions if a.startswith("enable --now") and a.split()[-1] in ENGINE_UNITS
    ]
    assert "restart containerd and docker" not in capsys.readouterr().out


def test_a_changed_daemon_config_warns_and_never_restarts(
    host_for, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.deploy()
    (host.build_dir / "daemon.json").write_text('{"log-driver": "local"}\n', encoding="utf-8")

    host.deploy()

    assert host.dest("daemon.json").read_text(encoding="utf-8") == '{"log-driver": "local"}\n'
    assert host.systemctl.actions() == []
    assert "restart containerd and docker" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["docker-timeout.conf", "containerd-override.conf"])
def test_a_changed_drop_in_reloads_systemd_but_never_restarts(
    host_for, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for()
    host.deploy()
    (host.build_dir / name).write_text("[Service]\n", encoding="utf-8")

    host.deploy()

    assert host.systemctl.actions() == ["daemon-reload"]
    assert "restart containerd and docker" in capsys.readouterr().out


def test_a_stale_deb822_source_is_replaced_and_kept_aside(host_for) -> None:
    """cottonwood after its release upgrade: a disabled `noble` docker.sources."""
    host = host_for()
    host.docker_list.unlink()
    stale = "Types: deb\nSuites: noble\nEnabled: no\n"
    host.docker_sources.write_text(stale, encoding="utf-8")

    host.deploy()

    assert not host.docker_sources.exists()
    assert (host.backup_dir / "docker.sources").read_text(encoding="utf-8") == stale
    assert host.docker_list.read_text(encoding="utf-8") == host.source()
    # Lists marked stale, but nothing is missing, so the network is not touched.
    assert not host.run.ran("apt-get")


def test_a_changed_source_refreshes_the_lists_before_installing(host_for) -> None:
    host = host_for()
    host.deploy()
    host.os_release.write_text("ID=debian\nVERSION_CODENAME=forky\n", encoding="utf-8")
    host.run.installed.discard("docker-compose-plugin")
    packages._apt_updated = True  # as if an earlier step in this run had refreshed

    host.installer.install(host.ctx())

    calls = [c for c in host.run.calls if c[:1] == ["apt-get"]]
    assert calls[0][-2:] == ["update", "-qq"]
    assert "docker-compose-plugin" in calls[1]
    assert "forky" in host.docker_list.read_text(encoding="utf-8")


@pytest.mark.parametrize("release", ["ID=fedora\nVERSION_CODENAME=x\n", "ID=debian\n", ""])
def test_an_unsupported_os_fails_before_anything_is_written(host_for, release: str) -> None:
    host = host_for()
    host.os_release.write_text(release, encoding="utf-8")
    before = host.snapshot()

    with pytest.raises(InstallError, match="unsupported OS"):
        host.deploy()

    assert host.snapshot() == before
    assert host.systemctl.calls == []


def test_a_failed_architecture_probe_fails_before_anything_is_written(host_for) -> None:
    host = host_for()
    host.run.answer("dpkg", "--print-architecture", code=2)
    before = host.snapshot()

    with pytest.raises(InstallError, match="print-architecture"):
        host.deploy()

    assert host.snapshot() == before


def test_snap_docker_is_removed(host_for) -> None:
    host = host_for()
    host.binaries.add("snap")

    host.deploy()

    assert ["snap", "remove", "--purge", "docker"] in host.run.calls


def test_no_snap_docker_is_left_alone(host_for) -> None:
    host = host_for()
    host.binaries.add("snap")
    host.run.answer("snap", "list", "docker", code=1)

    host.deploy()

    assert not host.run.ran("snap", "remove")


def test_a_failed_snap_removal_fails_the_deploy(host_for) -> None:
    host = host_for()
    host.binaries.add("snap")
    host.run.answer("snap", "remove", code=1)

    with pytest.raises(InstallError, match="snap remove --purge docker failed"):
        host.deploy()


def test_the_group_user_is_added_to_the_docker_group(host_for) -> None:
    host = host_for()
    host.docker_group.gr_mem = []

    host.deploy()

    assert ["usermod", "-aG", "docker", "freender"] in host.run.calls


def test_a_primary_docker_group_counts_as_membership(host_for) -> None:
    host = host_for()
    host.docker_group.gr_mem = []
    host.users["freender"].pw_gid = host.docker_group.gr_gid

    host.deploy()

    assert not host.run.ran("usermod")


def test_root_is_never_added_to_the_docker_group(host_for) -> None:
    host = host_for()
    host.env["DOCKER_GROUP_USER"] = "root"
    host.docker_group = None

    host.deploy()

    assert not host.run.ran("usermod")


def test_a_missing_docker_group_fails_the_deploy(host_for) -> None:
    host = host_for()
    host.docker_group = None

    with pytest.raises(InstallError, match="docker group does not exist"):
        host.deploy()


def test_a_failed_usermod_fails_the_deploy(host_for) -> None:
    host = host_for()
    host.docker_group.gr_mem = []
    host.run.answer("usermod", code=6)

    with pytest.raises(InstallError, match="usermod -aG docker freender failed"):
        host.deploy()


def test_a_map_without_the_key_fails_before_anything_is_written(host_for) -> None:
    host = host_for()
    del host.file_map["docker.asc"]
    before = host.snapshot()

    with pytest.raises(InstallError, match="missing file-map entry: docker.asc"):
        host.deploy()

    assert host.snapshot() == before
