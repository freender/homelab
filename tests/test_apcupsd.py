"""apcupsd: orchestrator file map and remote installer (freender/homelab-ops#30).

Every destination is rebound under `tmp_path`, and systemctl, dpkg/apt and
apcaccess are faked. Nothing here touches `/etc`, a real unit, or a UPS.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import apcupsd
from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.main import _parse_file_map

INSTALLER_PATH = Path(__file__).resolve().parents[1] / "apcupsd" / "scripts" / "install.py"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "has_rearm"),
    [("master", True), ("slave", True), ("master-standalone", False)],
)
def test_file_map_matches_what_was_rendered(tmp_path: Path, role: str, has_rearm: bool) -> None:
    """The map the installer reads and the files actually rendered must agree,
    or `files.install` raises on a missing source (or silently skips a file)."""
    root = Path(__file__).resolve().parents[1]
    templates = tmp_path / "apcupsd" / "templates"
    templates.parent.mkdir(parents=True)
    templates.symlink_to(root / "apcupsd" / "templates")

    build_dir = apcupsd.render_configs(
        tmp_path, "h", role, "ups", "bray:3551", "127.0.0.1", "ace clovis"
    )

    file_map = _parse_file_map(build_dir / "file-map.conf")
    rendered = {p.name for p in build_dir.iterdir()} - {"env", "file-map.conf"}
    assert set(file_map) == rendered
    assert ("homelab-ha-rearm.service" in file_map) is has_rearm
    assert file_map["doshutdown"] == ("/etc/apcupsd/doshutdown", "755")
    assert file_map["apcupsd.conf"] == ("/etc/apcupsd/apcupsd.conf", "644")


def test_orchestrator_stages_the_python_installer() -> None:
    assert apcupsd.INSTALLER == "scripts/install.py"
    assert apcupsd.INTERPRETER == "python3"


def test_role_sets_agree_across_the_wire() -> None:
    installer = load_installer()
    assert set(installer.ROLES) == set(apcupsd.ROLES)
    assert installer.HA_ROLES == apcupsd.HA_ROLES


# ---------------------------------------------------------------------------
# Remote installer
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("apcupsd_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSystemctl:
    def __init__(self, enabled: set[str] | None = None, active: set[str] | None = None) -> None:
        self.enabled = enabled if enabled is not None else {"apcupsd", "homelab-ha-rearm.service"}
        self.active = active if active is not None else {"apcupsd"}
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb = command[1]
        if verb == "is-enabled":
            code = 0 if command[-1] in self.enabled else 1
        elif verb == "is-active":
            code = 0 if command[-1] in self.active else 3
        else:
            code = 0
        return subprocess.CompletedProcess(command, code, "", "")

    def actions(self) -> list[str]:
        return [
            " ".join(call[1:])
            for call in self.calls
            if call[1] not in {"is-enabled", "is-active", "reset-failed"}
        ]


class FakeApt:
    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = missing or set()
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "dpkg-query":
            if command[-1] in self.missing:
                return subprocess.CompletedProcess(command, 1, "")
            return subprocess.CompletedProcess(command, 0, "install ok installed")
        if command[:2] == ["apt-get", "install"]:
            self.missing -= set(command[4:])
        return subprocess.CompletedProcess(command, 0, "")


class Host:
    """A sandboxed host filesystem plus a staged bundle for one role."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "hostfs"
        self.systemctl = FakeSystemctl()
        self.apt = FakeApt()
        self.apcaccess: list[list[str]] = []
        monkeypatch.setattr(systemd, "_run", self.systemctl)
        monkeypatch.setattr(packages, "_run", self.apt)
        monkeypatch.setattr(packages, "_apt_updated", False)
        monkeypatch.setattr(self.installer, "_run", self._apcaccess)

        inst = self.installer
        inst.HA_REARM_SCRIPT_PATH = self.under(inst.HA_REARM_SCRIPT_PATH)
        inst.HA_REARM_UNIT_PATH = self.under(inst.HA_REARM_UNIT_PATH)
        inst.APCCONTROL = self.under(inst.APCCONTROL)
        inst.DEFAULTS_FILE = self.under(inst.DEFAULTS_FILE)

        apccontrol = Path(inst.APCCONTROL)
        apccontrol.parent.mkdir(parents=True)
        apccontrol.write_text("#!/bin/sh\n", encoding="utf-8")
        apccontrol.chmod(0o755)
        defaults = Path(inst.DEFAULTS_FILE)
        defaults.parent.mkdir(parents=True)
        defaults.write_text("# comment\nISCONFIGURED=yes\n", encoding="utf-8")

        self.build_dir = tmp_path / "remote" / "build" / "h"
        self.build_dir.mkdir(parents=True)
        specs = apcupsd.file_specs(role)
        for spec in specs:
            source = self.build_dir / spec.build_name
            source.write_text(f"# {spec.build_name}\n", encoding="utf-8")
        self.file_map = {
            spec.build_name: (self.under(spec.remote_path), spec.mode) for spec in specs
        }
        self.role = role

    def under(self, dest: str) -> str:
        return str(self.root / dest.lstrip("/"))

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def _apcaccess(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.apcaccess.append(list(command))
        return subprocess.CompletedProcess(command, 0, "STATUS   : ONLINE\nUPSNAME  : ups\n", "")

    def ctx(self, env: dict[str, str] | None = None) -> InstallContext:
        return InstallContext(
            host="h",
            script_dir=self.build_dir.parents[1],
            build_dir=self.build_dir,
            env={"ROLE": self.role, "HOST": "h"} if env is None else env,
            deploy_env={},
            file_map=self.file_map,
            force_update=False,
        )

    def deploy(self, env: dict[str, str] | None = None) -> None:
        self.systemctl.calls.clear()
        self.installer.install(self.ctx(env))

    def converge(self) -> None:
        """Put the host in the state a previous deploy would have left."""
        self.deploy()
        for name in self.file_map:
            assert self.dest(name).read_bytes() == (self.build_dir / name).read_bytes()


@pytest.fixture
def host_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return lambda role: Host(tmp_path, monkeypatch, role)


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_a_fresh_cluster_node_gets_every_file_at_its_mode(host_for) -> None:
    host = host_for("slave")
    host.deploy()

    assert mode_of(host.dest("doshutdown")) == 0o755
    assert mode_of(host.dest("apcupsd.conf")) == 0o644
    assert mode_of(host.dest("homelab-ha-rearm")) == 0o755
    assert mode_of(host.dest("homelab-ha-rearm.service")) == 0o644


def test_the_ha_rearm_is_enabled_and_never_started(host_for) -> None:
    """Starting it would run `arm-ha` on a cluster that may be disarmed on purpose."""
    host = host_for("master")
    host.systemctl.enabled = {"apcupsd"}
    host.deploy()

    actions = host.systemctl.actions()
    assert "enable homelab-ha-rearm.service" in actions
    assert not any("homelab-ha-rearm" in a and ("start" in a or "--now" in a) for a in actions)


def test_an_unchanged_redeploy_restarts_nothing(host_for) -> None:
    host = host_for("slave")
    host.converge()
    host.deploy()

    assert host.systemctl.actions() == []
    assert list(host.dest("apcupsd.conf").parent.glob("apcupsd.conf.bak.*")) == []


def test_a_changed_conf_restarts_apcupsd_and_keeps_a_backup(host_for) -> None:
    host = host_for("slave")
    host.converge()
    (host.build_dir / "apcupsd.conf").write_text("# new\n", encoding="utf-8")
    host.deploy()

    assert "restart apcupsd" in host.systemctl.actions()
    assert len(list(host.dest("apcupsd.conf").parent.glob("apcupsd.conf.bak.*"))) == 1


def test_a_changed_doshutdown_does_not_restart_apcupsd(host_for) -> None:
    """apccontrol executes the hook afresh per event; nothing caches it."""
    host = host_for("slave")
    host.converge()
    (host.build_dir / "doshutdown").write_text("# new hook\n", encoding="utf-8")
    host.deploy()

    assert host.dest("doshutdown").read_text(encoding="utf-8") == "# new hook\n"
    assert "restart apcupsd" not in host.systemctl.actions()


def test_a_changed_rearm_unit_is_reloaded_but_not_restarted(host_for) -> None:
    host = host_for("master")
    host.converge()
    (host.build_dir / "homelab-ha-rearm.service").write_text("# v2\n", encoding="utf-8")
    host.deploy()

    assert host.systemctl.actions() == ["daemon-reload"]


def test_standalone_retires_a_leftover_rearm(host_for) -> None:
    host = host_for("master-standalone")
    script = Path(host.installer.HA_REARM_SCRIPT_PATH)
    unit = Path(host.installer.HA_REARM_UNIT_PATH)
    for path in (script, unit):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n", encoding="utf-8")
    host.systemctl.enabled = {"apcupsd", "homelab-ha-rearm.service"}

    host.deploy()

    assert not script.exists() and not unit.exists()
    assert "disable --now homelab-ha-rearm.service" in host.systemctl.actions()


def test_standalone_without_a_rearm_touches_nothing(host_for) -> None:
    """osiris's normal state: no unit, no script, nothing to reload."""
    host = host_for("master-standalone")
    host.systemctl.enabled = {"apcupsd"}
    host.converge()
    host.deploy()

    assert host.systemctl.actions() == []


@pytest.mark.parametrize("env", [{}, {"ROLE": ""}, {"ROLE": "unknown"}, {"ROLE": "Slave"}])
def test_a_bad_role_fails_before_anything_is_touched(host_for, env: dict[str, str]) -> None:
    """The bash sent an unknown role down the standalone branch and retired the
    HA re-arm on a cluster node."""
    host = host_for("slave")
    with pytest.raises(InstallError):
        host.deploy(env)

    assert host.systemctl.calls == []
    assert not host.dest("apcupsd.conf").exists()


def test_a_missing_package_is_installed(host_for) -> None:
    host = host_for("slave")
    host.apt.missing = {"apcupsd"}
    host.deploy()

    assert any(call[:2] == ["apt-get", "install"] and "apcupsd" in call for call in host.apt.calls)


def test_a_non_executable_apccontrol_is_fixed(host_for) -> None:
    host = host_for("slave")
    Path(host.installer.APCCONTROL).chmod(0o644)
    host.deploy()

    assert mode_of(Path(host.installer.APCCONTROL)) == 0o755


def test_a_missing_apccontrol_fails(host_for) -> None:
    host = host_for("slave")
    Path(host.installer.APCCONTROL).unlink()
    with pytest.raises(InstallError, match="apccontrol"):
        host.deploy()


def test_isconfigured_no_is_flipped_and_nothing_else_changes(host_for) -> None:
    host = host_for("slave")
    defaults = Path(host.installer.DEFAULTS_FILE)
    defaults.write_text(
        "# ISCONFIGURED=no stays a comment\nISCONFIGURED=no\nX=1\n", encoding="utf-8"
    )
    host.deploy()

    assert defaults.read_text(encoding="utf-8") == (
        "# ISCONFIGURED=no stays a comment\nISCONFIGURED=yes\nX=1\n"
    )


def test_a_missing_defaults_file_is_not_an_error(host_for) -> None:
    host = host_for("slave")
    Path(host.installer.DEFAULTS_FILE).unlink()
    host.deploy()


def test_slaves_do_not_query_a_nis_server_they_do_not_run(host_for) -> None:
    host = host_for("slave")
    host.deploy()
    assert host.apcaccess == []


def test_masters_report_ups_status(host_for, capsys: pytest.CaptureFixture[str]) -> None:
    host = host_for("master")
    host.deploy()
    assert host.apcaccess == [["apcaccess", "status"]]
    assert "STATUS   : ONLINE" in capsys.readouterr().out


def test_an_unreachable_ups_is_not_a_failed_deploy(
    host_for, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host = host_for("master-standalone")
    monkeypatch.setattr(
        host.installer, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "")
    )
    host.deploy()
    assert "Waiting for UPS connection" in capsys.readouterr().out


def test_blank_fields_right_after_a_restart_read_as_waiting(
    host_for, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """apcaccess answers with keys but no values before its first UPS poll."""
    host = host_for("master")
    monkeypatch.setattr(
        host.installer,
        "_run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "STATUS   : \nBCHARGE  :\n", ""),
    )
    host.deploy()
    out = capsys.readouterr().out
    assert "Waiting for UPS connection" in out
    assert "STATUS" not in out
