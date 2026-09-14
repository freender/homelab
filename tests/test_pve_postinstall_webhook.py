"""Remote installer for pve-postinstall-webhook (freender/homelab-ops#30).

Every destination is rebound onto a `tmp_path`, and both subprocess surfaces --
`packages._run` (dpkg/apt) and `systemd._run` (systemctl) -- are faked. Nothing
here touches `/etc`, `/root`, a real unit, or the network.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

INSTALLER_PATH = (
    Path(__file__).resolve().parents[1] / "pve-postinstall-webhook" / "scripts" / "install.py"
)


def load_installer():
    """Fresh load per test: these rebind the module's destination constants."""
    spec = importlib.util.spec_from_file_location("postinstall_webhook_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSystemctl:
    """Records systemctl invocations. `enabled`/`active` drive the query verbs,
    and `failures` makes one verb exit non-zero."""

    def __init__(
        self,
        enabled: set[str] | None = None,
        active: set[str] | None = None,
        failures: dict[str, int] | None = None,
    ) -> None:
        self.enabled = enabled if enabled is not None else set()
        self.active = active if active is not None else set()
        self.failures = failures or {}
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb = command[1]
        if verb == "is-enabled":
            return self._code(command, 0 if command[-1] in self.enabled else 1)
        if verb == "is-active":
            return self._code(command, 0 if command[-1] in self.active else 1)
        return self._code(command, self.failures.get(verb, 0))

    @staticmethod
    def _code(command: list[str], code: int) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, code, stdout="", stderr="")

    def actions(self) -> list[str]:
        """Every state-changing call, as `"<verb> <unit>"`."""
        return [
            f"{call[1]} {call[-1]}"
            for call in self.calls
            if call[1] not in {"is-enabled", "is-active", "daemon-reload"}
        ]


class FakeApt:
    """Reports every queried package installed unless it is in `missing`."""

    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = missing or set()
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "dpkg-query":
            name = command[-1]
            status = "" if name in self.missing else "install ok installed"
            return subprocess.CompletedProcess(command, 1 if name in self.missing else 0, status)
        return subprocess.CompletedProcess(command, 0, "")


def retarget(installer, tmp_path: Path) -> Path:
    """Rebind every absolute destination under a writable sandbox root."""
    sandbox = tmp_path / "hostfs"

    def under(dest: str) -> str:
        return str(sandbox / dest.lstrip("/"))

    installer.DIRECTORIES = tuple((under(path), mode) for path, mode in installer.DIRECTORIES)
    installer.POLLER_SCRIPTS = tuple(
        (name, under(dest), mode) for name, dest, mode in installer.POLLER_SCRIPTS
    )
    installer.AGENT_FILES = tuple(
        (name, under(dest), mode) for name, dest, mode in installer.AGENT_FILES
    )
    installer.UNIT_DIR = under(installer.UNIT_DIR)
    installer.POLLER_ENV_DEST = under(installer.POLLER_ENV_DEST)
    return sandbox


def make_ctx(installer, tmp_path: Path, poller_env: str = "REPO_DIR=/root/homelab\n"):
    """Stage every source file the installer expects and build its context."""
    script_dir = tmp_path / "remote"
    scripts = script_dir / "scripts"
    scripts.mkdir(parents=True)

    names = [name for name, _dest, _mode in (*installer.POLLER_SCRIPTS, *installer.AGENT_FILES)]
    for name in [*names, *installer.UNITS]:
        (scripts / name).write_text(f"# {name}\n", encoding="utf-8")

    build_dir = script_dir / "build" / "arc"
    build_dir.mkdir(parents=True)
    (build_dir / installer.POLLER_ENV_NAME).write_text(poller_env, encoding="utf-8")

    return InstallContext(
        host="arc",
        script_dir=script_dir,
        build_dir=build_dir,
        env={},
        deploy_env={},
        file_map={},
        force_update=False,
    )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    installer = load_installer()
    sandbox = retarget(installer, tmp_path)
    systemctl = FakeSystemctl()
    apt = FakeApt()
    monkeypatch.setattr(systemd, "_run", systemctl)
    monkeypatch.setattr(packages, "_run", apt)
    monkeypatch.setattr(packages, "_apt_updated", False)
    return installer, make_ctx(installer, tmp_path), sandbox, systemctl, apt


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def redeploy(ctx: InstallContext) -> InstallContext:
    """A second deploy against the same host: same staging, fresh ChangeSet.

    Rebuilt rather than reset, because a real redeploy is a new installer process
    -- carrying the previous run's recorded changes forward would make an
    unchanged file look changed and mask exactly the restart bug tested below.
    """
    return InstallContext(
        host=ctx.host,
        script_dir=ctx.script_dir,
        build_dir=ctx.build_dir,
        env=ctx.env,
        deploy_env=ctx.deploy_env,
        file_map=ctx.file_map,
        force_update=ctx.force_update,
    )


def test_everything_lands_at_its_pinned_mode(harness) -> None:
    """Pinned rather than left to the umask. `/root/.local/bin` holds the
    1Password loader and `poller.env` holds a live API token."""
    installer, ctx, sandbox, _systemctl, _apt = harness

    installer.install(ctx)

    assert mode_of(sandbox / "root/.local/bin") == 0o700
    assert mode_of(sandbox / "var/lib/homelab-postinstall-webhook/events") == 0o700
    assert mode_of(sandbox / "var/lib/homelab-postinstall-webhook/state") == 0o755
    assert mode_of(sandbox / "usr/local/sbin/homelab-postinstall-deploy") == 0o755
    assert mode_of(sandbox / "root/.config/op-ssh-agent.env") == 0o600
    assert mode_of(Path(installer.POLLER_ENV_DEST)) == 0o600


def test_the_poller_env_is_installed_from_the_build_directory(harness) -> None:
    """It is the only per-host rendered file in this module, and it is a secret:
    the orchestrator renders it in tmpfs and stages it straight through."""
    installer, ctx, _sandbox, _systemctl, _apt = harness

    installer.install(ctx)

    assert Path(installer.POLLER_ENV_DEST).read_text(encoding="utf-8") == "REPO_DIR=/root/homelab\n"


def test_an_unrotated_token_does_not_rewrite_the_env_file(harness) -> None:
    """The bash wrote it with `install` on every run. Rewriting a secret that did
    not change is needless exposure, and hides a real rotation in the log."""
    installer, ctx, _sandbox, _systemctl, _apt = harness
    installer.install(ctx)
    first = Path(installer.POLLER_ENV_DEST).stat().st_mtime_ns

    second = redeploy(ctx)
    installer.install(second)

    assert Path(installer.POLLER_ENV_DEST).stat().st_mtime_ns == first
    assert not second.changes.touched(installer.POLLER_ENV_NAME)


def test_a_changed_unit_is_restarted(harness) -> None:
    """The bash ran `systemctl enable --now`, which starts a stopped unit and does
    nothing at all to a running one -- so an edited `homelab-ssh-agent.service`
    kept running under its old definition until somebody noticed."""
    installer, ctx, _sandbox, systemctl, _apt = harness
    installer.install(ctx)

    systemctl.enabled = set(installer.ENABLED_UNITS)
    systemctl.active = set(installer.ENABLED_UNITS)
    systemctl.calls.clear()
    agent = ctx.script_dir / "scripts" / "homelab-ssh-agent.service"
    agent.write_text("# edited\n", encoding="utf-8")

    installer.install(redeploy(ctx))

    assert "restart homelab-ssh-agent.service" in systemctl.actions()
    assert "restart homelab-op-ssh-load.timer" not in systemctl.actions()


def test_an_unchanged_run_restarts_nothing(harness) -> None:
    installer, ctx, _sandbox, systemctl, _apt = harness
    installer.install(ctx)

    systemctl.enabled = set(installer.ENABLED_UNITS)
    systemctl.active = set(installer.ENABLED_UNITS)
    systemctl.calls.clear()

    installer.install(redeploy(ctx))

    assert [action for action in systemctl.actions() if action.startswith("restart")] == []


def test_the_key_load_runs_after_the_agent_is_up(harness) -> None:
    """Load-bearing ordering: restarting the agent drops the keys it held, and
    this is what puts them back. Reversing the two leaves an empty agent."""
    installer, ctx, _sandbox, systemctl, _apt = harness

    installer.install(ctx)

    actions = systemctl.actions()
    assert actions.index("enable homelab-ssh-agent.service") < actions.index(
        f"start {installer.KEY_LOAD_UNIT}"
    )


def test_a_failed_key_load_fails_the_deploy(harness) -> None:
    """A deploy that installed the loader and left the agent empty would look
    successful and then poll forever without ever deploying anything."""
    installer, ctx, _sandbox, systemctl, _apt = harness
    systemctl.failures = {"start": 1}

    with pytest.raises(InstallError, match=installer.KEY_LOAD_UNIT):
        installer.install(ctx)


def test_the_watch_service_is_installed_but_never_enabled(harness) -> None:
    """It is a oneshot the timer invokes; enabling it would also run it at boot,
    which is a full repo deploy on every reboot of the PDM host."""
    installer, ctx, _sandbox, systemctl, _apt = harness

    installer.install(ctx)

    unit = "homelab-pdm-installation-watch.service"
    assert Path(installer.UNIT_DIR, unit).is_file()
    assert f"enable {unit}" not in systemctl.actions()
    assert "enable homelab-pdm-installation-watch.timer" in systemctl.actions()


def test_a_changed_unit_is_reloaded_before_the_key_load_starts(harness) -> None:
    """`run_once` never passes through `ensure_running`, so a change to the
    key-load service alone would otherwise be started from systemd's cache."""
    installer, ctx, _sandbox, systemctl, _apt = harness
    installer.install(ctx)

    systemctl.enabled = set(installer.ENABLED_UNITS)
    systemctl.active = set(installer.ENABLED_UNITS)
    systemctl.calls.clear()
    unit = ctx.script_dir / "scripts" / installer.KEY_LOAD_UNIT
    unit.write_text("# edited\n", encoding="utf-8")

    installer.install(redeploy(ctx))

    verbs = [call[1] for call in systemctl.calls]
    assert "daemon-reload" in verbs
    assert verbs.index("daemon-reload") < verbs.index("start")


def test_missing_packages_are_installed_by_name(harness) -> None:
    """Checked per package through dpkg rather than by probing one binary on
    PATH: `python3-yaml` installs no binary at all, so a PATH probe for it has to
    be a `python3 -c 'import yaml'` side-channel, which is what the bash did."""
    installer, ctx, _sandbox, _systemctl, apt = harness
    apt.missing = {"python3-yaml"}

    with pytest.raises(InstallError, match="python3-yaml"):
        installer.install(ctx)

    installs = [call for call in apt.calls if call[:2] == ["apt-get", "install"]]
    assert installs and installs[0][-1] == "python3-yaml"


def test_a_partial_bundle_fails_before_any_unit_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing sources mean a partial upload. Installing the half that arrived
    and then bouncing the agent would be worse than not deploying at all."""
    installer = load_installer()
    retarget(installer, tmp_path)
    systemctl = FakeSystemctl()
    monkeypatch.setattr(systemd, "_run", systemctl)
    monkeypatch.setattr(packages, "_run", FakeApt())
    ctx = make_ctx(installer, tmp_path)
    (ctx.script_dir / "scripts" / "op-ssh-add").unlink()

    with pytest.raises(InstallError, match="missing source file"):
        installer.install(ctx)

    assert systemctl.actions() == []


def test_a_missing_rendered_env_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rendered env means the 1Password secret never resolved."""
    installer = load_installer()
    retarget(installer, tmp_path)
    monkeypatch.setattr(systemd, "_run", FakeSystemctl())
    monkeypatch.setattr(packages, "_run", FakeApt())
    ctx = make_ctx(installer, tmp_path)
    (ctx.build_dir / installer.POLLER_ENV_NAME).unlink()

    with pytest.raises(InstallError, match="missing source file"):
        installer.install(ctx)
