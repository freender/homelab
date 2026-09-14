"""docker-stacks: remote installer (freender/homelab-ops#30).

`docker` is replaced by a fake that answers `compose config --services` from the
staged YAML, lists containers from an in-memory table, and records `up -d`. Tests
assert on the host tree and on which stacks were brought up.

What carries the risk, and what these pin:

* **A converged host brings nothing up.** `up -d` on an unchanged stack is the
  deploy restarting production containers for no reason.
* **Every refusal leaves the host copy untouched**, and the other stacks still
  sync. A refused stack must never be half-installed.
* **Nothing the operator set on the host moves**: a `compose.yml`'s mode and owner,
  and the `.env` next to it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

from homelab.modules import docker_stacks
from homelab_install import files
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "docker-stacks" / "scripts" / "install.py"
DOCKER = "/usr/bin/docker"


def load_installer():
    spec = importlib.util.spec_from_file_location("docker_stacks_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose(*services: str, extra: str = "") -> str:
    body = "".join(f"  {name}:\n    image: busybox\n{extra}" for name in services)
    return f"services:\n{body}"


class FakeDocker:
    def __init__(self) -> None:
        # project -> [(service, container name)]
        self.containers: dict[str, list[tuple[str, str]]] = {}
        self.up: list[str] = []
        self.up_fails: set[str] = set()
        self.ps_fails = False
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        assert command[0] == DOCKER
        args = command[1:]
        if args[:2] == ["compose", "-f"] and args[-2:] == ["config", "--services"]:
            try:
                parsed = yaml.safe_load(Path(args[2]).read_text(encoding="utf-8"))
                services = list(parsed["services"])
            except Exception:
                return subprocess.CompletedProcess(
                    command, 15, "", "validating compose.yml: services must be a mapping\n"
                )
            return subprocess.CompletedProcess(command, 0, "\n".join(services) + "\n", "")
        if args[:2] == ["ps", "-a"]:
            if self.ps_fails:
                return subprocess.CompletedProcess(command, 1, "", "daemon down")
            project = args[3].rpartition("=")[2]
            rows = [f"{service}|{name}" for service, name in self.containers.get(project, [])]
            return subprocess.CompletedProcess(command, 0, "\n".join(rows), "")
        if args == ["compose", "up", "-d"]:
            stack = Path(kwargs["cwd"]).name
            self.up.append(stack)
            return subprocess.CompletedProcess(command, 1 if stack in self.up_fails else 0)
        raise AssertionError(f"unexpected docker call: {command}")


class Host:
    """A sandboxed appdata root plus a staged bundle."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installer = load_installer()
        self.docker = FakeDocker()
        monkeypatch.setattr(self.installer, "_run", self.docker)
        monkeypatch.setattr(self.installer, "_which", lambda _name: DOCKER)

        self.appdata = tmp_path / "appdata"
        self.appdata.mkdir()
        self.build_dir = tmp_path / "remote" / "build" / "h"
        (self.build_dir / "stacks").mkdir(parents=True)
        self.env: dict[str, str] = {"APPDATA_ROOT": str(self.appdata), "APPLY_CHANGED": "true"}
        self.deploy_env: dict[str, str] = {}
        self.force = False

    def stage(self, stack: str, text: str) -> None:
        path = self.build_dir / "stacks" / stack / "compose.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def live(self, stack: str, text: str | None = None, dotenv: str | None = None) -> Path:
        """Create the stack's appdata directory, optionally with a live copy."""
        directory = self.appdata / stack
        directory.mkdir(exist_ok=True)
        if text is not None:
            (directory / "compose.yml").write_text(text, encoding="utf-8")
        if dotenv is not None:
            (directory / ".env").write_text(dotenv, encoding="utf-8")
        return directory / "compose.yml"

    def ctx(self) -> InstallContext:
        env = dict(self.env)
        if "MANAGED_STACK_COUNT" not in env:
            env["MANAGED_STACK_COUNT"] = str(len(list((self.build_dir / "stacks").iterdir())))
        return InstallContext(
            host="h",
            script_dir=self.build_dir.parents[1],
            build_dir=self.build_dir,
            env=env,
            deploy_env=self.deploy_env,
            file_map={},
            force_update=self.force,
        )

    def deploy(self) -> None:
        self.docker.up.clear()
        self.installer.install(self.ctx())


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def test_orchestrator_stages_the_python_installer() -> None:
    assert docker_stacks.INSTALLER == "scripts/install.py"
    assert docker_stacks.INTERPRETER == "python3"
    assert (REPO_ROOT / "docker-stacks" / docker_stacks.INSTALLER).is_file()
    assert not (REPO_ROOT / "docker-stacks" / "scripts" / "install.sh").exists()


def test_the_env_keys_the_installer_requires_are_the_ones_the_orchestrator_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        docker_stacks, "write_env_file", lambda _path, values: captured.update(values)
    )
    monkeypatch.setattr(docker_stacks, "host_stacks", lambda _root, _host: [])
    monkeypatch.setattr(docker_stacks, "diff_many", lambda _conn, _pairs: [])

    class Registry:
        def get(self, _host: str, _key: str, default: object = None) -> object:
            return default if default is not None else "root"

    monkeypatch.setattr(docker_stacks, "default_registry", lambda _root: Registry())
    docker_stacks.deploy_host(tmp_path, "h", dry_run=True, force=False)

    assert set(captured) == {"APPDATA_ROOT", "APPLY_CHANGED", "MANAGED_STACK_COUNT"}


# ---------------------------------------------------------------------------
# Sync and apply
# ---------------------------------------------------------------------------


def test_a_converged_host_writes_nothing_and_brings_nothing_up(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.stage("plex", compose("plex"))
    dest = host.live("plex", compose("plex"))
    before = dest.stat().st_mtime_ns

    host.deploy()

    assert host.docker.up == []
    assert dest.stat().st_mtime_ns == before
    assert "managed=1 changed=0 applied=0 skipped=0 failed=0 unmanaged=0" in capsys.readouterr().out


def test_a_changed_stack_is_installed_and_brought_up(host: Host) -> None:
    host.stage("plex", compose("plex", extra="    restart: always\n"))
    host.stage("radarr", compose("radarr"))
    plex = host.live("plex", compose("plex"))
    host.live("radarr", compose("radarr"))

    host.deploy()

    assert plex.read_text(encoding="utf-8") == compose("plex", extra="    restart: always\n")
    assert host.docker.up == ["plex"]


def test_apply_disabled_syncs_the_file_without_reconciling(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.env["APPLY_CHANGED"] = "false"
    host.stage("plex", compose("plex", "sidecar"))
    dest = host.live("plex", compose("plex"))

    host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("plex", "sidecar")
    assert host.docker.up == []
    assert "apply disabled, not reconciled" in capsys.readouterr().out


def test_force_brings_up_every_stack_even_when_unchanged(host: Host) -> None:
    """Same as the bash: `file_needs_update` answered "changed" under FORCE_UPDATE."""
    host.force = True
    host.stage("plex", compose("plex"))
    host.live("plex", compose("plex"))

    host.deploy()

    assert host.docker.up == ["plex"]


def test_a_changed_file_keeps_its_mode_and_owner(host: Host) -> None:
    """The live files are freender-owned at 644, 664 and 775 on helm/neo/tower.
    `cp` never touched either; a mode rewrite would be a deploy-wide drift."""
    host.stage("grafana", compose("grafana", "renderer"))
    dest = host.live("grafana", compose("grafana"))
    dest.chmod(0o775)

    host.deploy()

    assert dest.stat().st_mode & 0o777 == 0o775


def test_a_new_file_takes_the_stack_directory_owner(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    chowned: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        host.installer.os, "chown", lambda path, uid, gid: chowned.append((str(path), uid, gid))
    )
    host.stage("seerr", compose("seerr"))
    directory = host.live("seerr").parent

    host.deploy()

    owner = directory.stat()
    assert chowned == [(str(directory / "compose.yml"), owner.st_uid, owner.st_gid)]
    assert host.docker.up == ["seerr"]


def test_an_existing_file_is_never_chowned(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.installer.os, "chown", lambda *_args: pytest.fail("chowned"))
    host.stage("plex", compose("plex", "extra"))
    host.live("plex", compose("plex"))

    host.deploy()


def test_a_failed_apply_fails_the_deploy_after_the_other_stacks(host: Host) -> None:
    host.stage("plex", compose("plex", "a"))
    host.stage("radarr", compose("radarr", "a"))
    host.live("plex", compose("plex"))
    host.live("radarr", compose("radarr"))
    host.docker.up_fails.add("plex")

    with pytest.raises(InstallError, match="1 stack\\(s\\) failed"):
        host.deploy()

    assert host.docker.up == ["plex", "radarr"]


def test_the_env_file_is_never_written(host: Host) -> None:
    host.stage("plex", compose("plex", "a"))
    dest = host.live("plex", compose("plex"), dotenv="TOKEN=secret\n")
    env_file = dest.parent / ".env"
    before = (env_file.read_bytes(), env_file.stat().st_mtime_ns)

    host.deploy()

    assert (env_file.read_bytes(), env_file.stat().st_mtime_ns) == before


def test_unmanaged_stacks_are_reported_and_left_alone(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.stage("plex", compose("plex"))
    host.live("plex", compose("plex"))
    handmade = host.live("handmade", compose("handmade"))

    host.deploy()

    out = capsys.readouterr().out
    assert "unmanaged stacks on host (not in repo, left untouched): handmade" in out
    assert "unmanaged=1" in out
    assert handmade.is_file()


# ---------------------------------------------------------------------------
# Refusals: host copy untouched, other stacks still sync, deploy fails
# ---------------------------------------------------------------------------


def test_a_stack_with_no_appdata_directory_is_refused_not_created(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.stage("ghost", compose("ghost"))
    host.stage("real", compose("real"))
    host.live("real")

    with pytest.raises(InstallError, match="1 stack\\(s\\) skipped"):
        host.deploy()

    assert not (host.appdata / "ghost").exists(), "installer created the directory"
    assert (host.appdata / "real" / "compose.yml").is_file()
    assert host.docker.up == ["real"]
    out = capsys.readouterr().out
    assert "does not exist on this host" in out
    assert "applied=1 skipped=1" in out


def test_an_undefined_variable_is_refused(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.stage("traefik", compose("traefik", extra='    labels: ["Host(`x.${DOMAIN}`)"]\n'))
    dest = host.live("traefik", compose("traefik"))

    with pytest.raises(InstallError, match="skipped"):
        host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("traefik")
    assert "needs undefined variable(s): DOMAIN" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("dotenv", "deploy_env"),
    [
        ("DOMAIN=example.net\n", {}),
        ("export DOMAIN=example.net\n", {}),
        ("  DOMAIN=\n", {}),
        ("", {"DOMAIN": "example.net"}),
    ],
)
def test_a_variable_defined_in_env_or_the_environment_is_accepted(
    host: Host, dotenv: str, deploy_env: dict[str, str]
) -> None:
    host.deploy_env.update(deploy_env)
    host.stage("traefik", compose("traefik", extra='    labels: ["${DOMAIN}"]\n'))
    host.live("traefik", compose("traefik"), dotenv=dotenv)

    host.deploy()

    assert host.docker.up == ["traefik"]


def test_an_empty_process_variable_does_not_count_as_defined(host: Host) -> None:
    host.deploy_env["DOMAIN"] = ""
    host.stage("traefik", compose("traefik", extra='    labels: ["${DOMAIN}"]\n'))
    host.live("traefik", compose("traefik"))

    with pytest.raises(InstallError, match="skipped"):
        host.deploy()


def test_a_commented_out_key_does_not_define_a_variable(host: Host) -> None:
    host.stage("traefik", compose("traefik", extra='    labels: ["${DOMAIN}"]\n'))
    host.live("traefik", compose("traefik"), dotenv="# DOMAIN=example.net\nDOMAINS=x\n")

    with pytest.raises(InstallError, match="skipped"):
        host.deploy()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("${A}", {"A"}),
        ("$${A}", set()),
        ("$$${A}", {"A"}),
        ("${A:-x} ${B:?required} $C", set()),
        ("x${A}y${B_2}", {"A", "B_2"}),
    ],
)
def test_escaped_references_are_not_variables(text: str, expected: set[str]) -> None:
    """`$${VAR}` is how alertmanager and immich pass a literal to the container
    shell. The bash grep counted it as a variable compose needed."""
    assert load_installer().referenced_variables(text) == expected


def test_a_definition_compose_rejects_is_not_installed(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bash copied it anyway, replacing the working file start.sh reads."""
    host.stage("plex", "services: [broken\n")
    dest = host.live("plex", compose("plex"))

    with pytest.raises(InstallError, match="skipped"):
        host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("plex")
    assert host.docker.up == []
    assert "docker compose config rejected the definition" in capsys.readouterr().out


def test_config_is_resolved_against_the_stack_directory(host: Host) -> None:
    host.stage("plex", compose("plex"))
    dest = host.live("plex", compose("plex"))

    host.deploy()

    config = next(call for call in host.docker.calls if call[-1] == "--services")
    assert config[config.index("--project-directory") + 1] == str(dest.parent)


def test_a_renamed_service_with_a_live_container_is_refused(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.stage("crowdsec", compose("crowdsec"))
    dest = host.live("crowdsec", compose("crowdsec-lapi"))
    host.docker.containers["crowdsec"] = [("crowdsec-lapi", "crowdsec-lapi-1")]
    host.docker.containers["other"] = [("unrelated", "unrelated-1")]

    with pytest.raises(InstallError, match="skipped"):
        host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("crowdsec-lapi")
    assert host.docker.up == []
    assert "docker rm -f crowdsec-lapi-1" in capsys.readouterr().out


def test_containers_of_services_still_defined_are_not_stale(host: Host) -> None:
    host.stage("crowdsec", compose("crowdsec", "bouncer"))
    host.live("crowdsec", compose("crowdsec"))
    host.docker.containers["crowdsec"] = [("crowdsec", "crowdsec")]

    host.deploy()

    assert host.docker.up == ["crowdsec"]


def test_a_failed_docker_ps_fails_the_stack_instead_of_skipping_the_check(host: Host) -> None:
    host.stage("plex", compose("plex", "a"))
    dest = host.live("plex", compose("plex"))
    host.docker.ps_fails = True

    with pytest.raises(InstallError, match="1 stack\\(s\\) failed"):
        host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("plex")


def test_a_failed_write_fails_the_stack(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(files, "install_from", refuse)
    host.stage("plex", compose("plex", "a"))
    host.live("plex", compose("plex"))

    with pytest.raises(InstallError, match="failed"):
        host.deploy()

    assert host.docker.up == []


# ---------------------------------------------------------------------------
# Up-front refusals: nothing touched at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["APPDATA_ROOT", "APPLY_CHANGED"])
def test_a_truncated_env_file_is_refused(host: Host, key: str) -> None:
    host.stage("plex", compose("plex"))
    del host.env[key]

    with pytest.raises(InstallError, match=f"missing: {key}"):
        host.deploy()


def test_a_typoed_apply_flag_is_refused(host: Host) -> None:
    host.env["APPLY_CHANGED"] = "ture"
    host.stage("plex", compose("plex"))

    with pytest.raises(InstallError, match="APPLY_CHANGED must be true or false"):
        host.deploy()


def test_a_partial_upload_is_refused(host: Host) -> None:
    host.stage("plex", compose("plex", "a"))
    dest = host.live("plex", compose("plex"))
    host.env["MANAGED_STACK_COUNT"] = "2"

    with pytest.raises(InstallError, match="staged 1 stack\\(s\\) but the orchestrator rendered 2"):
        host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("plex")


def test_a_non_integer_stack_count_is_refused(host: Host) -> None:
    host.stage("plex", compose("plex"))
    host.env["MANAGED_STACK_COUNT"] = "many"

    with pytest.raises(InstallError, match="must be an integer"):
        host.deploy()


def test_a_missing_appdata_root_is_refused(host: Host) -> None:
    host.stage("plex", compose("plex"))
    host.env["APPDATA_ROOT"] = str(host.appdata / "absent")

    with pytest.raises(InstallError, match="missing appdata root"):
        host.deploy()


def test_a_missing_stacks_directory_is_refused(host: Host) -> None:
    (host.build_dir / "stacks").rmdir()
    host.env["MANAGED_STACK_COUNT"] = "0"

    with pytest.raises(InstallError, match="missing staged stacks directory"):
        host.deploy()


def test_missing_docker_is_refused_before_any_write(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host.installer, "_which", lambda _name: None)
    host.stage("plex", compose("plex", "a"))
    dest = host.live("plex", compose("plex"))

    with pytest.raises(InstallError, match="docker not found"):
        host.deploy()

    assert dest.read_text(encoding="utf-8") == compose("plex")
