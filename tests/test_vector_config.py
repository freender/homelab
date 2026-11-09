from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from homelab.modules import vector_config
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def host_entry(name: str, stacks: list[str], feature: str | None = "") -> str:
    """`feature` is the vector-config block body; None leaves the feature off."""
    stack_lines = "".join(f"        - {stack}\n" for stack in stacks)
    block = "" if feature is None else f"    vector-config:\n{feature}"
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
        f"{block}"
    )


def fake_root(tmp_path: Path, *hosts: str) -> Path:
    (tmp_path / "hosts.conf").write_text("".join(hosts), encoding="utf-8")
    module = tmp_path / vector_config.MODULE_DIR
    shutil.copytree(ROOT / vector_config.MODULE_DIR / "templates", module / "templates")
    (module / "scripts").mkdir(parents=True)
    (module / "scripts" / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return tmp_path


def render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path, host: str) -> Path:
    """Rendered through deploy_host's dry run, so tests see exactly the bytes that
    would be diffed and shipped."""
    monkeypatch.setattr(vector_config, "connection_for_host", lambda _root, _host: None)
    monkeypatch.setattr(vector_config, "diff_many", lambda _conn, _pairs: [])
    vector_config.deploy_host(root, host, dry_run=True, force=False)
    return root / vector_config.MODULE_DIR / "build" / host


def real_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / vector_config.MODULE_DIR).mkdir(parents=True)
    shutil.copy(ROOT / "hosts.conf", root / "hosts.conf")
    templates = vector_config.MODULE_DIR + "/templates"
    shutil.copytree(ROOT / templates, root / templates)
    return root


def test_validate_accepts_the_real_inventory() -> None:
    vector_config.validate(ROOT)


def test_validate_refuses_the_feature_without_the_stack(tmp_path: Path) -> None:
    """No container means nothing to validate against or reload."""
    root = fake_root(tmp_path, host_entry("box", ["traefik"]))

    with pytest.raises(ValueError, match="enabled without the vector stack on: box"):
        vector_config.validate(root)


def test_validate_refuses_the_stack_without_the_feature(tmp_path: Path) -> None:
    root = fake_root(tmp_path, host_entry("box", ["vector"], feature=None))

    with pytest.raises(ValueError, match="vector stack runs without vector-config on: box"):
        vector_config.validate(root)


def test_validate_refuses_a_non_boolean_shadow(tmp_path: Path) -> None:
    """`shadow: "false"` would be truthy and keep a cut-over host on tenant 1."""
    root = fake_root(tmp_path, host_entry("box", ["vector"], "      shadow: 'false'\n"))

    with pytest.raises(ValueError, match="box: vector-config.shadow must be true or false"):
        vector_config.validate(root)


def test_shadow_defaults_off(tmp_path: Path) -> None:
    root = fake_root(
        tmp_path,
        host_entry("plain", ["vector"]),
        host_entry("shadowed", ["vector"], "      shadow: true\n"),
    )

    assert vector_config.shadow_enabled(root, "plain") is False
    assert vector_config.shadow_enabled(root, "shadowed") is True


def test_geoip_follows_the_geoipupdate_stack(tmp_path: Path) -> None:
    root = fake_root(
        tmp_path,
        host_entry("geo", ["vector", "geoipupdate"]),
        host_entry("plain", ["vector"]),
    )

    assert vector_config.geoip_enabled(root, "geo") is True
    assert vector_config.geoip_enabled(root, "plain") is False


@pytest.mark.parametrize(("host", "geoip"), [("tower", True), ("helm", True), ("neo", False)])
def test_real_hosts_render_their_own_name_and_geoip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, geoip: bool
) -> None:
    build = render(tmp_path, monkeypatch, real_repo(tmp_path), host)

    text = (build / "vector.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    assert config["sources"]["docker"]["exclude_containers"] == [f"vector-{host}"]
    shape = config["transforms"]["docker_shape"]["source"]
    assert f'if name == "traefik-{host}" {{' in shape
    assert f'"host": "{host}",' in shape
    assert f'"host": "{host}",' in config["transforms"]["varlogs_shape"]["source"]
    assert ("enrichment_tables" in config) is geoip
    assert ("get_enrichment_table_record" in shape) is geoip
    assert "{%" not in text

    tests = (build / "vector.test.yaml").read_text(encoding="utf-8")
    assert f"container_name: traefik-{host}\n" in tests
    assert ('"Google LLC"' in tests) is geoip
    assert "{{" not in tests and "{%" not in tests

    env_text = (build / "env").read_text(encoding="utf-8")
    assert f"VECTOR_CONTAINER=vector-{host}" in env_text
    assert "VECTOR_DEST=/mnt/cache/appdata/vector/vector.yaml" in env_text
    geoip_dir = "/mnt/cache/appdata/geoipupdate" if geoip else "''"
    assert f"VECTOR_GEOIP_DIR={geoip_dir}\n" in env_text


def test_sinks_keep_the_field_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The #363 contract: the Loki endpoint (its JSON auto-parse feeds every
    Traefik panel), labels job/host/container_name only, varlogs on a sink of its
    own, disk buffers that block, and acks only where the source can honour them."""
    build = render(tmp_path, monkeypatch, real_repo(tmp_path), "tower")
    sinks = yaml.safe_load((build / "vector.yaml").read_text(encoding="utf-8"))["sinks"]

    docker, varlogs = sinks["vlogs_docker"], sinks["vlogs_varlogs"]
    for sink in (docker, varlogs):
        assert sink["type"] == "loki"
        assert sink["path"] == "/insert/loki/api/v1/push"
        assert sink["encoding"] == {"codec": "text"}
        assert sink["buffer"]["type"] == "disk"
        assert sink["buffer"]["when_full"] == "block"
    assert set(docker["labels"]) == {"job", "host", "container_name"}
    assert docker["labels"]["container_name"] == "{{ container_name }}"
    assert set(varlogs["labels"]) == {"job", "host"}
    assert "acknowledgements" not in docker
    assert varlogs["acknowledgements"] == {"enabled": True}
    assert sinks["metrics"]["address"] == "0.0.0.0:9598"


@pytest.mark.parametrize("shadow", [True, False])
def test_shadow_alone_picks_the_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: bool
) -> None:
    feature = "      shadow: true\n" if shadow else ""
    root = fake_root(tmp_path, host_entry("box", ["vector"], feature))
    build = render(tmp_path, monkeypatch, root, "box")
    sinks = yaml.safe_load((build / "vector.yaml").read_text(encoding="utf-8"))["sinks"]

    for name in ("vlogs_docker", "vlogs_varlogs"):
        assert sinks[name].get("tenant_id") == ("1" if shadow else None)


def test_real_inventory_is_shadowed_until_cutover() -> None:
    """Remove with the shadow keys at cutover (homelab-ops#367)."""
    for host in ("tower", "helm", "neo"):
        assert vector_config.shadow_enabled(ROOT, host) is True


def vector_binary() -> str | None:
    found = os.environ.get("VECTOR_BIN") or shutil.which("vector")
    if found:
        return found
    cached = Path.home() / ".cache/vector-sync/0.61.0/vector"
    return str(cached) if cached.is_file() else None


@pytest.mark.skipif(vector_binary() is None, reason="no vector binary (VECTOR_BIN or PATH)")
def test_neo_render_passes_vector_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer runs these in the host's image on every deploy; this catches a
    broken template before that. neo, because it needs no GeoIP database."""
    build = render(tmp_path, monkeypatch, real_repo(tmp_path), "neo")
    config = tmp_path / "vector.yaml"
    config.write_text(
        (build / "vector.yaml")
        .read_text(encoding="utf-8")
        .replace("/var/lib/vector", str(tmp_path)),
        encoding="utf-8",
    )
    binary = vector_binary()
    assert binary
    for args in (["validate", "--no-environment", str(config)],
                 ["test", str(config), str(build / "vector.test.yaml")]):
        result = subprocess.run([binary, *args], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Remote installer -- docker faked, live file in tmp_path
# ---------------------------------------------------------------------------

INSTALLER_PATH = ROOT / "vector-config" / "scripts" / "install.py"
RELOADED = "INFO vector: Vector has reloaded. path=[File(\"/etc/vector/vector.yaml\", None)]"


def load_installer():
    spec = importlib.util.spec_from_file_location("vector_config_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    def __init__(
        self,
        validate_code: int = 0,
        test_code: int = 0,
        kill_code: int = 0,
        inspect_code: int = 0,
        image: str = "timberio/vector:0.61.0-debian",
        reload_log: str = RELOADED,
    ) -> None:
        self.validate_code = validate_code
        self.test_code = test_code
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
            code = self.validate_code if "validate" in command else self.test_code
            return subprocess.CompletedProcess(command, code, "x bad input", "")
        if verb == "kill":
            return subprocess.CompletedProcess(command, self.kill_code, "", "")
        if verb == "logs":
            # docker logs splits the container's stdout and stderr; read both.
            return subprocess.CompletedProcess(command, 0, self.reload_log, "")
        raise AssertionError(f"unexpected command: {command}")

    def runs(self) -> list[list[str]]:
        return [call for call in self.calls if call[1] == "run"]

    def ran(self, verb: str) -> bool:
        return any(call[1] == verb for call in self.calls)


def setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeDocker,
    staged: str,
    live: str | None,
    geoip_dir: str = "",
):
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", fake)
    monkeypatch.setattr(installer, "_sleep", lambda _seconds: None)
    build = tmp_path / "build" / "box"
    build.mkdir(parents=True)
    (build / "vector.yaml").write_text(staged, encoding="utf-8")
    (build / "vector.test.yaml").write_text("tests: []\n", encoding="utf-8")
    dest = tmp_path / "live" / "vector.yaml"
    if live is not None:
        dest.parent.mkdir(parents=True)
        dest.write_text(live, encoding="utf-8")
    ctx = InstallContext(
        host="box",
        script_dir=tmp_path,
        build_dir=build,
        env={
            "VECTOR_CONTAINER": "vector-box",
            "VECTOR_DEST": str(dest),
            "VECTOR_GEOIP_DIR": geoip_dir,
        },
        deploy_env={},
        file_map={},
        force_update=False,
    )
    return installer, ctx, dest


def test_changed_config_is_checked_installed_and_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(image="timberio/vector:0.62.0-debian")
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n", "/geo")

    installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "new\n"
    validate, unit = fake.runs()
    build = ctx.build_dir
    for run in (validate, unit):
        assert run[:5] == ["docker", "run", "--rm", "--name", "vector-box-check"]
        assert "timberio/vector:0.62.0-debian" in run
        assert f"{build}/vector.yaml:/etc/vector/vector.yaml:ro" in run
        assert f"{build}/vector.test.yaml:/etc/vector/vector.test.yaml:ro" in run
        assert "/geo:/etc/vector/geoip:ro" in run
    assert validate[-3:] == ["validate", "--no-environment", "/etc/vector/vector.yaml"]
    assert unit[-3:] == ["test", "/etc/vector/vector.yaml", "/etc/vector/vector.test.yaml"]
    assert ["docker", "kill", "--signal", "HUP", "vector-box"] in fake.calls


def test_no_geoip_dir_mounts_no_geoip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    installer.install(ctx)

    assert not any("/etc/vector/geoip" in arg for run in fake.runs() for arg in run)


def test_the_file_is_written_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """vector.yaml is a single-file bind mount; a rename would leave the container
    reading the old inode forever."""
    installer, ctx, dest = setup(tmp_path, monkeypatch, FakeDocker(), "new\n", "old\n")
    inode = dest.stat().st_ino

    installer.install(ctx)

    assert dest.stat().st_ino == inode


def test_unchanged_config_is_checked_but_not_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "same\n", "same\n")

    installer.install(ctx)

    assert len(fake.runs()) == 2
    assert not fake.ran("kill")


@pytest.mark.parametrize(
    ("fake", "message", "runs"),
    [
        (FakeDocker(validate_code=78), "vector rejected the staged config", 1),
        (FakeDocker(test_code=1), "vector unit tests failed", 2),
    ],
)
def test_a_failed_check_is_never_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    fake: FakeDocker,
    message: str,
    runs: int,
) -> None:
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "bad\n", "old\n")

    with pytest.raises(InstallError, match=message):
        installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "old\n"
    assert len(fake.runs()) == runs
    assert not fake.ran("kill")
    assert "bad input" in capsys.readouterr().out


def test_a_missing_live_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing file means the appdata mount is missing, not a fresh host."""
    fake = FakeDocker()
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "new\n", None)

    with pytest.raises(InstallError, match="missing live Vector config"):
        installer.install(ctx)

    assert not dest.exists()
    assert fake.calls == []


@pytest.mark.parametrize("missing", ["vector.yaml", "vector.test.yaml"])
def test_a_missing_staged_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    fake = FakeDocker()
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")
    (ctx.build_dir / missing).unlink()

    with pytest.raises(InstallError, match=f"missing staged file: .*{missing}"):
        installer.install(ctx)

    assert fake.calls == []


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeDocker(inspect_code=1), "container is missing: vector-box"),
        (FakeDocker(image=""), "could not read the image of vector-box"),
    ],
)
def test_no_image_means_no_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDocker, message: str
) -> None:
    installer, ctx, dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    with pytest.raises(InstallError, match=message):
        installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "old\n"
    assert not fake.runs()


@pytest.mark.parametrize(
    "reload_log",
    [
        # Captured from Vector 0.61 on neo: a config that fails to load.
        'ERROR vector::internal_events::process: Failed to load config files, reload aborted.',
        "ERROR vector: Reload was not successful.",
        "ERROR vector: Fatal error during reload.",
    ],
)
def test_a_rejected_reload_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reload_log: str
) -> None:
    """Vector keeps running the old config when a reload fails; only its log says so."""
    fake = FakeDocker(reload_log=reload_log)
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")

    with pytest.raises(InstallError, match="rejected the new config and kept the old one"):
        installer.install(ctx)


def test_reload_output_is_read_from_both_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(reload_log="")
    installer, ctx, _dest = setup(tmp_path, monkeypatch, fake, "new\n", "old\n")
    original = fake.__call__

    def stderr_logs(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        if command[1] == "logs":
            fake.calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", RELOADED)
        return original(command, **kwargs)

    monkeypatch.setattr(installer, "_run", stderr_logs)

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

    with pytest.raises(InstallError, match="failed to signal vector-box"):
        installer.install(ctx)

    assert not fake.ran("logs")
