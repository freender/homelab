from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from homelab.modules import alloy_config
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def host_entry(name: str, stacks: list[str], alloy_config_enabled: bool) -> str:
    stack_lines = "".join(f"        - {stack}\n" for stack in stacks)
    feature = "    alloy-config:\n" if alloy_config_enabled else ""
    return (
        f"{name}:\n"
        "  config:\n"
        "    type: ubuntu\n"
        f"    hostname: {name}.internal\n"
        "    user: root\n"
        "    sshkey: infra\n"
        "  features:\n"
        "    docker-stacks:\n"
        "      stacks:\n"
        f"{stack_lines}"
        f"{feature}"
    )


def fake_root(tmp_path: Path, *hosts: str) -> Path:
    (tmp_path / "hosts.conf").write_text("".join(hosts), encoding="utf-8")
    module = tmp_path / alloy_config.MODULE_DIR
    shutil.copytree(ROOT / alloy_config.MODULE_DIR / "templates", module / "templates")
    (module / "scripts").mkdir(parents=True)
    (module / "scripts" / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return tmp_path


def test_validate_accepts_the_real_inventory() -> None:
    alloy_config.validate(ROOT)


def test_validate_refuses_the_feature_without_the_stack(tmp_path: Path) -> None:
    """No container means nothing to validate against or reload."""
    root = fake_root(tmp_path, host_entry("box", ["traefik"], alloy_config_enabled=True))

    with pytest.raises(ValueError, match="enabled without the alloy stack on: box"):
        alloy_config.validate(root)


def test_validate_refuses_the_stack_without_the_feature(tmp_path: Path) -> None:
    """The drift this module exists to end: Alloy shipping logs from a config in
    no repo."""
    root = fake_root(tmp_path, host_entry("box", ["alloy"], alloy_config_enabled=False))

    with pytest.raises(ValueError, match="alloy stack runs without alloy-config on: box"):
        alloy_config.validate(root)


def test_geoip_follows_the_geoipupdate_stack(tmp_path: Path) -> None:
    root = fake_root(
        tmp_path,
        host_entry("geo", ["alloy", "geoipupdate"], alloy_config_enabled=True),
        host_entry("plain", ["alloy"], alloy_config_enabled=True),
    )

    assert alloy_config.geoip_enabled(root, "geo") is True
    assert alloy_config.geoip_enabled(root, "plain") is False


@pytest.mark.parametrize(
    ("host", "geoip"), [("tower", True), ("helm", True), ("neo", False)]
)
def test_real_hosts_render_their_own_name_and_geoip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, geoip: bool
) -> None:
    """Rendered through deploy_host's dry run, so the test sees exactly the bytes
    that would be diffed and shipped."""
    monkeypatch.setattr(alloy_config, "connection_for_host", lambda _root, _host: None)
    monkeypatch.setattr(alloy_config, "diff_many", lambda _conn, _pairs: [])
    root = tmp_path / "repo"
    (root / alloy_config.MODULE_DIR).mkdir(parents=True)
    shutil.copy(ROOT / "hosts.conf", root / "hosts.conf")
    templates = alloy_config.MODULE_DIR + "/templates"
    shutil.copytree(ROOT / templates, root / templates)

    alloy_config.deploy_host(root, host, dry_run=True, force=False)

    build = root / alloy_config.MODULE_DIR / "build" / host
    text = (build / "config.alloy").read_text(encoding="utf-8")
    assert f'regex         = "/traefik-{host}"' in text
    assert f'loki.source.docker "traefik_{host}_logs"' in text
    assert f'host = "{host}",' in text  # varlogs static label
    assert f'replacement  = "{host}"\n\t\ttarget_label = "host"' in text  # docker relabel
    assert ("stage.geoip" in text) is geoip
    assert "{{" not in text and "{%" not in text

    env_text = (build / "env").read_text(encoding="utf-8")
    assert f"ALLOY_CONTAINER=alloy-{host}" in env_text
    assert f"ALLOY_DEST={alloy_config.REMOTE_CONFIG}" in env_text


def test_varlogs_uses_static_labels() -> None:
    """stage.labels reads values from extracted fields, so `job = "varlogs"` there
    looked up a field named varlogs and labelled nothing (homelab-ops#342)."""
    template_path = ROOT / alloy_config.MODULE_DIR / "templates" / alloy_config.TEMPLATE_NAME
    template = template_path.read_text(encoding="utf-8")
    pipeline = template.split('loki.process "system_logs_pipeline"', 1)[1].split("\n}\n", 1)[0]
    assert "stage.static_labels" in pipeline
    assert "stage.labels" not in pipeline


# ---------------------------------------------------------------------------
# Remote installer -- docker faked, live file in tmp_path
# ---------------------------------------------------------------------------

INSTALLER_PATH = ROOT / "alloy-config" / "scripts" / "install.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("alloy_config_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    def __init__(
        self,
        validate_code: int = 0,
        kill_code: int = 0,
        inspect_code: int = 0,
        image: str = "grafana/alloy:latest",
        reload_log: str = 'level=info msg="config reloaded"',
    ) -> None:
        self.validate_code = validate_code
        self.kill_code = kill_code
        self.inspect_code = inspect_code
        self.image = image
        self.reload_log = reload_log
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb = command[1]
        if verb == "inspect":
            return subprocess.CompletedProcess(command, self.inspect_code, f"{self.image}\n", "")
        if verb == "run":
            return subprocess.CompletedProcess(command, self.validate_code, "", "Error: bad block")
        if verb == "kill":
            return subprocess.CompletedProcess(command, self.kill_code, "", "")
        if verb == "logs":
            # Alloy logs to stderr; the installer must read both streams.
            return subprocess.CompletedProcess(command, 0, "", self.reload_log)
        raise AssertionError(f"unexpected command: {command}")

    def ran(self, verb: str) -> bool:
        return any(call[1] == verb for call in self.calls)


def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDocker, staged: str, live: str | None
):
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", fake)
    monkeypatch.setattr(installer, "_sleep", lambda _seconds: None)
    build = tmp_path / "build" / "box"
    build.mkdir(parents=True)
    (build / "config.alloy").write_text(staged, encoding="utf-8")
    dest = tmp_path / "live" / "config.alloy"
    if live is not None:
        dest.parent.mkdir(parents=True)
        dest.write_text(live, encoding="utf-8")
    ctx = InstallContext(
        host="box",
        script_dir=tmp_path,
        build_dir=build,
        env={"ALLOY_CONTAINER": "alloy-box", "ALLOY_DEST": str(dest)},
        deploy_env={},
        file_map={},
        force_update=False,
    )
    return installer, ctx, dest


def test_changed_config_is_validated_installed_and_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(image="grafana/alloy:v1.21.3")
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "new\n"
    run = next(call for call in fake.calls if call[1] == "run")
    assert "grafana/alloy:v1.21.3" in run
    assert run[-2:] == ["validate", "/etc/alloy/config.alloy"]
    assert ["docker", "kill", "--signal", "HUP", "alloy-box"] in fake.calls


def test_the_file_is_written_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config.alloy is a single-file bind mount; a rename would leave the
    container reading the old inode forever."""
    installer, ctx, dest = setup(tmp_path, monkeypatch, FakeDocker(), "new\n", "old\n")
    inode = dest.stat().st_ino

    installer.install(ctx)

    assert dest.stat().st_ino == inode


def test_unchanged_config_is_not_reloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "same\n", "same\n")

    installer.install(ctx)

    assert fake.ran("run")
    assert not fake.ran("kill")


def test_a_rejected_config_is_never_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake = FakeDocker(validate_code=1)
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "bad\n", "old\n")

    with pytest.raises(InstallError, match="alloy rejected the staged config"):
        installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "old\n"
    assert not fake.ran("kill")
    assert "bad block" in capsys.readouterr().out


def test_a_missing_live_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing file means the appdata mount is missing, not a fresh host."""
    fake = FakeDocker()
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "new\n", None)

    with pytest.raises(InstallError, match="missing live Alloy config"):
        installer.install(ctx)

    assert not dest.exists()
    assert fake.calls == []


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeDocker(inspect_code=1), "container is missing: alloy-box"),
        (FakeDocker(image=""), "could not read the image of alloy-box"),
    ],
)
def test_no_image_means_no_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDocker, message: str
) -> None:
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    with pytest.raises(InstallError, match=message):
        installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "old\n"


def test_a_rejected_reload_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alloy keeps running the old config when a reload fails; only its log says so."""
    fake = FakeDocker(reload_log='level=error msg="failed to reload config" err="boom"')
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    with pytest.raises(InstallError, match="rejected the new config and kept the old one"):
        installer.install(ctx)


def test_a_silent_reload_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker(reload_log="")
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")
    clock = iter([0.0, 5.0, 10.0, 16.0])
    monkeypatch.setattr(installer, "_now", lambda: next(clock))

    with pytest.raises(InstallError, match="did not confirm a reload within 15s"):
        installer.install(ctx)

    assert sum(call[1] == "logs" for call in fake.calls) == 3


def test_a_failed_signal_fails_the_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker(kill_code=1)
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    with pytest.raises(InstallError, match="failed to signal alloy-box"):
        installer.install(ctx)

    assert not fake.ran("logs")
