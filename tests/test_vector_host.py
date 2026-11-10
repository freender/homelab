from __future__ import annotations

import grp
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from homelab.modules import vector_host
from homelab.templates import render_template_text
from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def host_entry(name: str, features: str = "") -> str:
    return (
        f"{name}:\n"
        "  config:\n"
        "    type: lxc\n"
        f"    hostname: {name}.internal\n"
        "    user: root\n"
        "    sshkey: infra\n"
        "  features:\n"
        f"{features}"
    )


LOGS = host_entry("logs", "    docker-stacks:\n      stacks:\n        - victoria-logs\n")


def fake_root(tmp_path: Path, *hosts: str) -> Path:
    (tmp_path / "hosts.conf").write_text("".join(hosts), encoding="utf-8")
    for module, subs in ((vector_host.MODULE_DIR, ("templates", "configs")),
                         ("vector-config", ("templates", "configs"))):
        for sub in subs:
            shutil.copytree(ROOT / module / sub, tmp_path / module / sub)
    scripts = tmp_path / vector_host.MODULE_DIR / "scripts"
    scripts.mkdir()
    (scripts / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return tmp_path


def validate(root: Path) -> None:
    from homelab.hosts import default_registry

    vector_host.validate(root, default_registry(root).list_hosts(vector_host.MODULE_DIR))


def test_validate_accepts_the_real_inventory() -> None:
    validate(ROOT)


@pytest.mark.parametrize(
    "missing",
    ["vector-host/templates/vector.yaml.j2", "vector-host/templates/vector.test.yaml.j2",
     "vector-host/templates/vector-override.conf.j2", "vector-host/configs/datadog.asc",
     "vector-host/scripts/install.py", "vector-config/templates/journald.yaml.j2",
     "vector-config/configs/journalctl-catchup"],
)
def test_validate_requires_every_module_file(tmp_path: Path, missing: str) -> None:
    root = fake_root(tmp_path, LOGS, host_entry("box", "    vector-host:\n"))
    (root / missing).unlink()

    with pytest.raises(ValueError, match=f"missing module file: .*{missing}"):
        validate(root)


def test_validate_needs_exactly_one_victoria_logs(tmp_path: Path) -> None:
    root = fake_root(tmp_path, host_entry("box", "    vector-host:\n"))

    with pytest.raises(ValueError, match="exactly one host running the victoria-logs stack"):
        validate(root)


@pytest.mark.parametrize(
    "features", ["    vector-host:\n      shadow: true\n", "    vector-host:\n"]
)
def test_validate_accepts_either_tenant(tmp_path: Path, features: str) -> None:
    validate(fake_root(tmp_path, LOGS, host_entry("box", features)))


def test_validate_refuses_a_bad_shadow_value(tmp_path: Path) -> None:
    root = fake_root(tmp_path, LOGS, host_entry("box", "    vector-host:\n      shadow: maybe\n"))

    with pytest.raises(ValueError, match="box: vector-host.shadow must be true or false"):
        validate(root)


def test_validate_refuses_a_docker_vector_host(tmp_path: Path) -> None:
    """Those ship their journal from the container (vector-config.journald)."""
    root = fake_root(tmp_path, LOGS, host_entry("box", "    vector-host:\n    vector-config:\n"))

    with pytest.raises(ValueError, match="box: runs the vector stack"):
        validate(root)


@pytest.mark.parametrize("value", ["'true'", "1", "[true]"])
def test_validate_refuses_a_non_boolean_shadow(tmp_path: Path, value: str) -> None:
    features = f"    vector-host:\n      shadow: {value}\n"
    root = fake_root(tmp_path, LOGS, host_entry("box", features))

    with pytest.raises(ValueError, match="box: vector-host.shadow must be true or false"):
        validate(root)


def build(root: Path, host: str = "box") -> Path:
    return vector_host.render_build(root, host)


@pytest.mark.parametrize(("shadow", "tenant"), [(True, "1"), (False, "0")])
def test_render_ships_the_journal_to_the_shadow_tenant_only_when_asked(
    tmp_path: Path, shadow: bool, tenant: str
) -> None:
    features = "    vector-host:\n" + ("      shadow: true\n" if shadow else "")
    out = build(fake_root(tmp_path, LOGS, host_entry("box", features)))
    config = yaml.safe_load((out / "vector.yaml").read_text(encoding="utf-8"))

    assert config["data_dir"] == "/var/lib/vector"
    assert set(config["sources"]) == {"journald", "internal"}
    assert config["sources"]["journald"]["journalctl_path"] == "/usr/local/bin/journalctl-catchup"
    sink = config["sinks"]["vlogs_journald"]
    assert sink["endpoints"] == ["http://logs.internal:9428/insert/elasticsearch/"]
    assert sink.get("request", {}).get("headers", {}).get("AccountID") == (
        "1" if shadow else None
    )
    assert config["sinks"]["metrics"]["address"] == "0.0.0.0:9598"
    env_text = (out / "env").read_text(encoding="utf-8")
    assert "VECTOR_QUERY_URL=http://logs.internal:9428/select/logsql/query\n" in env_text
    assert f"VECTOR_TENANT={tenant}\n" in env_text


def test_the_journal_pieces_are_vector_configs_byte_for_byte(tmp_path: Path) -> None:
    """One owner for the shape: the transform, sink settings and tests vector-host
    ships are what vector-config renders for a journald host."""
    root = fake_root(tmp_path, LOGS, host_entry("box", "    vector-host:\n"))
    host = yaml.safe_load((build(root) / "vector.yaml").read_text(encoding="utf-8"))
    docker = yaml.safe_load(render_template_text(
        ROOT / "vector-config/templates/vector.yaml.j2",
        HOST="box", GEOIP=False, SHADOW=False, JOURNALD="on",
    ))

    assert host["transforms"]["journald_shape"] == docker["transforms"]["journald_shape"]
    for key in ("query", "buffer", "acknowledgements", "healthcheck", "mode", "compression"):
        assert host["sinks"]["vlogs_journald"][key] == docker["sinks"]["vlogs_journald"][key]
    source = dict(host["sources"]["journald"], journalctl_path=None)
    assert source == dict(docker["sources"]["journald"], journalctl_path=None)
    host_tests = yaml.safe_load((build(root) / "vector.test.yaml").read_text(encoding="utf-8"))
    docker_tests = yaml.safe_load(render_template_text(
        ROOT / "vector-config/templates/vector.test.yaml.j2",
        HOST="box", GEOIP=False, SHADOW=False, JOURNALD="on",
    ))
    journal = [t for t in docker_tests["tests"] if t["name"].startswith("journal entry")]
    assert host_tests["tests"] == journal


def test_render_writes_every_file_the_map_names(tmp_path: Path) -> None:
    root = fake_root(tmp_path, LOGS, host_entry("box", "    vector-host:\n"))
    out = build(root)

    lines = (out / "file-map.conf").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "datadog.asc|/etc/apt/keyrings/datadog.asc|644",
        "vector.list|/etc/apt/sources.list.d/vector.list|644",
        "journalctl-catchup|/usr/local/bin/journalctl-catchup|755",
        "vector-override.conf|/etc/systemd/system/vector.service.d/homelab.conf|644",
        "vector.yaml|/etc/vector/homelab.yaml|644",
    ]
    for line in lines:
        assert (out / line.split("|")[0]).is_file()
    assert (out / "vector.list").read_text(encoding="utf-8") == (
        "deb [signed-by=/etc/apt/keyrings/datadog.asc] https://apt.vector.dev/ stable vector-0\n"
    )
    wrapper = out / "journalctl-catchup"
    assert wrapper.read_bytes() == (ROOT / "vector-config/configs/journalctl-catchup").read_bytes()
    assert os.access(wrapper, os.X_OK)


def test_the_unit_drop_in_survives_helm_being_down(tmp_path: Path) -> None:
    """#373 at boot: no start limit, a 30 s retry, validate without healthchecks,
    and the module's config instead of the package's conffile."""
    out = build(fake_root(tmp_path, LOGS, host_entry("box", "    vector-host:\n")))
    lines = (out / "vector-override.conf").read_text(encoding="utf-8").splitlines()

    assert lines.index("[Unit]") < lines.index("StartLimitIntervalSec=0") < lines.index("[Service]")
    start_pre = [line for line in lines if line.startswith("ExecStartPre=")]
    assert start_pre == [
        "ExecStartPre=",
        "ExecStartPre=/usr/bin/vector validate --skip-healthchecks",
    ]
    assert "RestartSec=30s" in lines
    assert "Environment=VECTOR_CONFIG=/etc/vector/homelab.yaml" in lines


def test_the_vendored_keyring_is_pinned() -> None:
    """Datadog's C0962C7D signing key (today's CURRENT, expires 2028) and its
    F14F620E master key; the first verifies apt.vector.dev's Release file."""
    key = (ROOT / "vector-host/configs/datadog.asc").read_bytes()
    assert hashlib.sha256(key).hexdigest() == (
        "13b1271c31d372e392e6167bd21c9712a06aeea7a560fe829e29bf166999f27e"
    )


def test_real_arc_render_passes_vector(tmp_path: Path) -> None:
    binary = os.environ.get("VECTOR_BIN") or shutil.which("vector")
    cached = Path.home() / ".cache/vector-sync/0.61.0/vector"
    binary = binary or (str(cached) if cached.is_file() else None)
    if not binary:
        pytest.skip("no vector binary (VECTOR_BIN or PATH)")
    root = tmp_path / "repo"
    for sub in ("vector-host/templates", "vector-host/configs", "vector-config/templates",
                "vector-config/configs"):
        shutil.copytree(ROOT / sub, root / sub)
    shutil.copy(ROOT / "hosts.conf", root / "hosts.conf")
    out = build(root, "arc")
    for args in (["validate", "--no-environment", str(out / "vector.yaml")],
                 ["test", str(out / "vector.yaml"), str(out / "vector.test.yaml")]):
        result = subprocess.run([binary, *args], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Remote installer -- apt, systemctl, vector, logger and VictoriaLogs faked
# ---------------------------------------------------------------------------

INSTALLER_PATH = ROOT / "vector-host" / "scripts" / "install.py"
STAGED = ("datadog.asc", "vector.list", "journalctl-catchup", "vector-override.conf",
          "vector.yaml", "vector.test.yaml")


def load_installer():
    spec = importlib.util.spec_from_file_location("vector_host_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHost:
    """dpkg, apt-get, systemctl, vector and logger in one double."""

    def __init__(self, installed: bool = True, active: bool = True, enabled: bool = True,
                 validate_code: int = 0, test_code: int = 0) -> None:
        self.installed = installed
        self.active = active
        self.enabled = enabled
        self.validate_code = validate_code
        self.test_code = test_code
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        name = command[0]
        if name == "dpkg-query":
            status = "install ok installed" if self.installed else ""
            return subprocess.CompletedProcess(command, 0 if self.installed else 1, status, "")
        if name == "apt-get":
            if "install" in command:
                self.installed = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if name == "systemctl":
            verb = command[1]
            if verb == "is-active":
                return subprocess.CompletedProcess(command, 0 if self.active else 3)
            if verb == "is-enabled":
                return subprocess.CompletedProcess(command, 0 if self.enabled else 1)
            if verb in ("enable", "start", "restart"):
                self.active = self.enabled = True
            return subprocess.CompletedProcess(command, 0)
        if name == "/usr/bin/vector":
            code = self.validate_code if command[1] == "validate" else self.test_code
            return subprocess.CompletedProcess(command, code, "x bad input", "")
        if name == "logger":
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(f"unexpected command: {command}")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)

    def index(self, *prefix: str) -> int:
        return next(i for i, call in enumerate(self.calls) if call[: len(prefix)] == list(prefix))


class FakeVictoriaLogs:
    def __init__(self, answers: list[object]) -> None:
        self.answers = list(answers)
        self.requests: list = []

    def __call__(self, request, timeout: float):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)


FOUND = b'{"_msg":"delivery check nonce"}\n'


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost,
          logs: FakeVictoriaLogs | None = None, live: dict[str, str] | None = None,
          journal_members: list[str] | None = None):
    installer = load_installer()
    for module in (installer, packages, systemd):
        monkeypatch.setattr(module, "_run", host)
    monkeypatch.setattr(packages, "_apt_updated", False)
    monkeypatch.setattr(installer, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(installer, "_nonce", lambda: "nonce")
    monkeypatch.setattr(installer, "_urlopen", logs or FakeVictoriaLogs([FOUND]))
    members = ["vector"] if journal_members is None else journal_members
    monkeypatch.setattr(
        installer.grp, "getgrnam", lambda name: grp.struct_group((name, "x", 999, members))
    )
    staged = tmp_path / "build" / "box"
    staged.mkdir(parents=True)
    file_map = {}
    for name in STAGED:
        (staged / name).write_text(f"{name} new\n", encoding="utf-8")
        if name != "vector.test.yaml":
            dest = tmp_path / "live" / name
            file_map[name] = (str(dest), "644")
            if live and name in live:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(live[name], encoding="utf-8")
    ctx = InstallContext(
        host="box",
        script_dir=tmp_path,
        build_dir=staged,
        env={"VECTOR_QUERY_URL": "http://logs:9428/select/logsql/query", "VECTOR_TENANT": "1"},
        deploy_env={},
        file_map=file_map,
        force_update=False,
    )
    return installer, ctx


CONVERGED = {name: f"{name} new\n" for name in STAGED}


def test_a_fresh_host_gets_repo_package_config_then_a_started_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(installed=False, active=False, enabled=False)
    logs = FakeVictoriaLogs([FOUND])
    installer, ctx = setup(tmp_path, monkeypatch, host, logs)

    installer.install(ctx)

    for name in STAGED[:-1]:
        assert (tmp_path / "live" / name).read_text(encoding="utf-8") == f"{name} new\n"
    install = next(call for call in host.calls if call[:1] == ["apt-get"] and "install" in call)
    assert install[-2:] == ["--no-install-recommends", "vector"]
    assert host.index("apt-get") < host.index("/usr/bin/vector", "validate")
    assert host.index("/usr/bin/vector", "test") < host.index("systemctl", "enable")
    assert ["systemctl", "enable", "--now", "vector.service"] in host.calls
    assert host.ran("logger", "-t", "vector-host", "delivery check nonce")
    request = logs.requests[0]
    assert request.get_header("Accountid") == "1"
    assert "delivery+check+nonce" in request.full_url


def test_the_wrapper_is_in_place_before_the_config_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost()
    installer, ctx = setup(tmp_path, monkeypatch, host)
    wrapper = Path(ctx.file_map["journalctl-catchup"][0])
    seen: list[bool] = []
    original = host.__call__

    def watch(command: list[str], **kwargs):
        if command[:2] == ["/usr/bin/vector", "validate"]:
            seen.append(wrapper.is_file())
        return original(command, **kwargs)

    monkeypatch.setattr(installer, "_run", watch)

    installer.install(ctx)

    assert seen == [True]


def test_a_converged_running_host_is_checked_but_not_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost()
    logs = FakeVictoriaLogs([FOUND])
    installer, ctx = setup(tmp_path, monkeypatch, host, logs, live=CONVERGED)

    installer.install(ctx)

    assert not host.ran("apt-get")
    assert host.ran("/usr/bin/vector", "validate")
    assert not host.ran("systemctl", "restart")
    assert not host.ran("logger")
    assert logs.requests == []


def test_a_stopped_converged_host_is_started_and_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(active=False)
    installer, ctx = setup(tmp_path, monkeypatch, host, live=CONVERGED)

    installer.install(ctx)

    assert ["systemctl", "start", "vector.service"] in host.calls
    assert host.ran("logger")


@pytest.mark.parametrize("changed", ["journalctl-catchup", "vector-override.conf", "vector.yaml"])
def test_any_changed_runtime_file_restarts_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    host = FakeHost()
    live = dict(CONVERGED, **{changed: "old\n"})
    installer, ctx = setup(tmp_path, monkeypatch, host, live=live)

    installer.install(ctx)

    assert host.index("systemctl", "daemon-reload") < host.index("systemctl", "restart")
    assert host.ran("logger")


def test_a_changed_apt_source_refreshes_the_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(installed=False)
    live = dict(CONVERGED, **{"vector.list": "old\n"})
    installer, ctx = setup(tmp_path, monkeypatch, host, live=live)
    monkeypatch.setattr(packages, "_apt_updated", True)

    installer.install(ctx)

    assert host.ran("apt-get", "-o", "DPkg::Lock::Timeout=600", "update")


@pytest.mark.parametrize(
    ("host", "message"),
    [(FakeHost(validate_code=78), "vector rejected the staged config"),
     (FakeHost(test_code=1), "vector unit tests failed")],
)
def test_a_failed_check_installs_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host: FakeHost, message: str
) -> None:
    live = dict(CONVERGED, **{"vector.yaml": "old\n", "vector-override.conf": "old\n"})
    installer, ctx = setup(tmp_path, monkeypatch, host, live=live)

    with pytest.raises(InstallError, match=message):
        installer.install(ctx)

    for name in ("vector.yaml", "vector-override.conf"):
        assert (tmp_path / "live" / name).read_text(encoding="utf-8") == "old\n"
    assert not host.ran("systemctl", "restart")
    assert "bad input" in capsys.readouterr().out


@pytest.mark.parametrize("missing", STAGED)
def test_a_missing_staged_file_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    host = FakeHost(installed=False)
    installer, ctx = setup(tmp_path, monkeypatch, host)
    (ctx.build_dir / missing).unlink()

    with pytest.raises(InstallError, match=f"missing staged file: .*{missing}"):
        installer.install(ctx)

    assert host.calls == []
    assert not (tmp_path / "live").exists()


def test_a_missing_file_map_entry_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(installed=False)
    installer, ctx = setup(tmp_path, monkeypatch, host)
    del ctx.file_map["vector.yaml"]

    with pytest.raises(InstallError, match="missing file-map entry: vector.yaml"):
        installer.install(ctx)

    assert host.calls == []


@pytest.mark.parametrize("missing", ["VECTOR_QUERY_URL", "VECTOR_TENANT"])
def test_an_incomplete_env_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    host = FakeHost()
    installer, ctx = setup(tmp_path, monkeypatch, host)
    del ctx.env[missing]

    with pytest.raises(InstallError, match=missing):
        installer.install(ctx)

    assert host.calls == []


def test_vector_outside_systemd_journal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It would run, read only its own lines, and nothing would fail."""
    host = FakeHost()
    installer, ctx = setup(tmp_path, monkeypatch, host, journal_members=["root"])

    with pytest.raises(InstallError, match="user vector is not in systemd-journal"):
        installer.install(ctx)

    assert not host.ran("/usr/bin/vector")


def test_a_missing_journal_group_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost()
    installer, ctx = setup(tmp_path, monkeypatch, host)

    def no_group(name: str):
        raise KeyError(name)

    monkeypatch.setattr(installer.grp, "getgrnam", no_group)

    with pytest.raises(InstallError, match="group systemd-journal does not exist"):
        installer.install(ctx)


def test_delivery_retries_markers_until_victoria_logs_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The first marker can predate the source; a query error is only a retry."""
    host = FakeHost(active=False)
    logs = FakeVictoriaLogs([b"", OSError("connection refused"), FOUND])
    installer, ctx = setup(tmp_path, monkeypatch, host, logs, live=CONVERGED)

    installer.install(ctx)

    assert sum(call[0] == "logger" for call in host.calls) == 3
    assert "connection refused" in capsys.readouterr().out


def test_no_delivery_fails_the_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = FakeHost(active=False)
    installer, ctx = setup(tmp_path, monkeypatch, host, FakeVictoriaLogs([b""]), live=CONVERGED)
    clock = iter([0.0, 10.0, 20.0, 31.0])
    monkeypatch.setattr(installer, "_now", lambda: next(clock))

    with pytest.raises(InstallError, match="delivered nothing within 30s"):
        installer.install(ctx)

    assert sum(call[0] == "logger" for call in host.calls) == 3
