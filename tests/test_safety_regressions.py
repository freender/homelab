from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from homelab import cli
from homelab.modules import (
    apt_upgrade,
    keepalived,
    pve_gpu_passthrough,
    pve_postinstall_webhook,
)

ROOT = Path(__file__).resolve().parents[1]


class KeyedRegistry:
    """Registry stub that answers exactly one key.

    Deliberately not a "return the same value for every key" stub: that shape cannot
    tell whether a normalizer reads the hosts.conf key it is supposed to, so a
    normalizer pointed at the wrong key would still pass.
    """

    def __init__(self, key: str, value: object) -> None:
        self.key = key
        self.value = value

    def get(self, _host: str, key: str, default: object = None) -> object:
        return self.value if key == self.key else default


# Each of these gates a destructive or noisy action, and each has a deliberately
# *safe* default when the key is absent: no unattended dist-upgrade, no host GPU
# torn away from the console, post-install deploy stays in dry-run.
DANGEROUS_BOOLEANS = [
    (apt_upgrade.normalize_autoupgrade, "apt-upgrade.autoupgrade", False),
    (
        pve_gpu_passthrough.normalize_isolate_host_gpu,
        "pve-gpu-passthrough.isolate_host_gpu",
        False,
    ),
    (
        pve_postinstall_webhook.normalize_deploy_dry_run,
        "pve-postinstall-webhook.dry_run",
        True,
    ),
]


@pytest.mark.parametrize(("normalizer", "key", "safe_default"), DANGEROUS_BOOLEANS)
def test_dangerous_boolean_settings_are_strict(normalizer, key: str, safe_default: bool) -> None:
    assert normalizer(KeyedRegistry(key, "false"), "ace") is False
    assert normalizer(KeyedRegistry(key, "true"), "ace") is True
    with pytest.raises(ValueError, match="must be true or false"):
        normalizer(KeyedRegistry(key, "ture"), "ace")


@pytest.mark.parametrize(("normalizer", "key", "safe_default"), DANGEROUS_BOOLEANS)
def test_dangerous_booleans_read_their_own_key(normalizer, key: str, safe_default: bool) -> None:
    """A normalizer wired to the wrong hosts.conf key silently ignores the operator.

    Answering only `key` means a normalizer reading anything else falls through to
    its default, so the `true` case below fails loudly instead of passing.
    """
    assert normalizer(KeyedRegistry(key, "true"), "ace") is True
    assert normalizer(KeyedRegistry("some.other.key", "true"), "ace") is safe_default


def test_postinstall_poller_env_excludes_retired_listener_values() -> None:
    env = pve_postinstall_webhook._env_values(
        "/root/homelab", "pdm-token", "false", "1200", "3600"
    )

    assert env["PDM_TOKEN_SECRET"] == "pdm-token"
    assert "WEBHOOK_TOKEN" not in env
    assert "LISTEN_HOST" not in env
    assert "LISTEN_PORT" not in env


@pytest.mark.parametrize(("normalizer", "key", "safe_default"), DANGEROUS_BOOLEANS)
def test_dangerous_booleans_default_to_the_safe_side(
    normalizer, key: str, safe_default: bool
) -> None:
    """An absent key must not enable the dangerous behavior.

    Note `dry_run` defaults True while the other two default False — the safe
    direction differs per setting, so this cannot be asserted generically.
    """
    assert normalizer(KeyedRegistry(key, None), "ace") is safe_default


def test_deploy_rejects_unknown_host_before_module_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "hosts.conf").write_text(
        """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features: {}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)

    result = CliRunner().invoke(cli.main, ["deploy", "docker", "orbit"])

    assert result.exit_code != 0
    assert "unknown host 'orbit'" in result.output


def test_pause_reports_systemd_disable_failure() -> None:
    script = f"""
source {shlex.quote(str(ROOT / 'lib' / 'utils.sh'))}
systemctl() {{
    case "$1" in
        is-active) return 0 ;;
        is-enabled) return 1 ;;
        disable) return 1 ;;
    esac
}}
homelab_apply_pause true homelab-test.timer
"""

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "failed to stop and disable homelab-test.timer" in result.stderr


def test_retire_systemd_unit_stops_and_removes_unit(tmp_path: Path) -> None:
    unit_path = tmp_path / "homelab-test.timer"
    unit_path.write_text("[Timer]\n", encoding="utf-8")
    log_path = tmp_path / "systemctl.log"
    script = f"""
source {shlex.quote(str(ROOT / 'lib' / 'utils.sh'))}
systemctl() {{
    printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
    case "$1" in
        is-enabled) return 0 ;;
        *) return 0 ;;
    esac
}}
retire_systemd_unit homelab-test.timer "$UNIT_PATH"
"""
    env = {
        **os.environ,
        "SYSTEMCTL_LOG": str(log_path),
        "UNIT_PATH": str(unit_path),
    }

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not unit_path.exists()
    log = log_path.read_text(encoding="utf-8")
    assert "disable --now homelab-test.timer" in log
    assert "daemon-reload" in log
    # A unit retired while failed must not linger in `systemctl --failed`.
    assert "reset-failed homelab-test.timer" in log


def run_utils_snippet(
    snippet: str, log_path: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with lib/utils.sh sourced and systemctl stubbed.

    The stub appends every invocation to $SYSTEMCTL_LOG so tests can assert on
    which systemctl calls were (and were not) made. Per-case behavior is driven
    by SYSTEMCTL_IS_ENABLED / SYSTEMCTL_UNIT_KNOWN.
    """
    script = f"""
source {shlex.quote(str(ROOT / 'lib' / 'utils.sh'))}
systemctl() {{
    printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
    case "$1" in
        list-unit-files) [[ "${{SYSTEMCTL_UNIT_KNOWN:-1}}" == "1" ]] && return 0 || return 1 ;;
        is-enabled)
            # Mirrors systemd: prints the state on stdout (read by the mask
            # helper) and exits non-zero unless actually enabled (checked by
            # retire_systemd_unit via --quiet).
            printf '%s\n' "${{SYSTEMCTL_IS_ENABLED:-enabled}}"
            [[ "${{SYSTEMCTL_IS_ENABLED:-enabled}}" == "enabled" ]] || return 1
            ;;
        is-active) return 1 ;;
    esac
    return 0
}}
{snippet}
"""
    env = {**os.environ, "SYSTEMCTL_LOG": str(log_path), **(extra_env or {})}
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_retire_systemd_unit_reports_nothing_to_do(tmp_path: Path) -> None:
    """Returns 1 when there is nothing to retire.

    Callers under `set -e` must consume this status; a bare call would abort
    the installer on the common no-op path.
    """
    log_path = tmp_path / "systemctl.log"

    result = run_utils_snippet(
        'retire_systemd_unit homelab-absent.timer "$UNIT_PATH"; echo "rc=$?"',
        log_path,
        {
            "UNIT_PATH": str(tmp_path / "does-not-exist.timer"),
            "SYSTEMCTL_IS_ENABLED": "disabled",
        },
    )

    assert "rc=1" in result.stdout, result.stderr
    assert "daemon-reload" not in log_path.read_text(encoding="utf-8")


def test_reload_and_clear_failed_is_inert_when_unchanged(tmp_path: Path) -> None:
    """The gate is the whole point: no change means no reload and no reset.

    An unconditional reset-failed would hide a real ongoing failure from
    failed-unit alerting until the next scheduled run.
    """
    log_path = tmp_path / "systemctl.log"
    log_path.touch()

    result = run_utils_snippet(
        "homelab_reload_and_clear_failed false homelab-test.service", log_path
    )

    assert result.returncode == 0, result.stderr
    assert log_path.read_text(encoding="utf-8") == ""


def test_reload_and_clear_failed_resets_changed_units(tmp_path: Path) -> None:
    log_path = tmp_path / "systemctl.log"

    result = run_utils_snippet(
        "homelab_reload_and_clear_failed true homelab-a.service homelab-b.timer",
        log_path,
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "daemon-reload" in log
    assert "reset-failed homelab-a.service" in log
    assert "reset-failed homelab-b.timer" in log


def test_mask_unwanted_service_masks_and_clears_failed(tmp_path: Path) -> None:
    log_path = tmp_path / "systemctl.log"

    result = run_utils_snippet(
        'homelab_mask_unwanted_service openipmi.service "no IPMI hardware"',
        log_path,
        {"SYSTEMCTL_IS_ENABLED": "enabled"},
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "mask openipmi.service" in log
    assert "reset-failed openipmi.service" in log
    assert "no IPMI hardware" in result.stdout


def test_mask_unwanted_service_clears_failed_when_already_masked(tmp_path: Path) -> None:
    """Idempotent re-run must still clear a stale failed record.

    A unit masked while failed keeps its failed record, which would otherwise
    trip a failed-unit alert forever.
    """
    log_path = tmp_path / "systemctl.log"

    result = run_utils_snippet(
        "homelab_mask_unwanted_service openipmi.service",
        log_path,
        {"SYSTEMCTL_IS_ENABLED": "masked"},
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "reset-failed openipmi.service" in log
    assert "mask openipmi.service" not in log


def run_recover_snippet(
    snippet: str,
    log_path: Path,
    tmp_path: Path,
    *,
    failed_units: str = "",
    start_rc: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Harness for homelab_recover_failed_units.

    The stub is a real executable on PATH, not a bash function: the helper runs
    `timeout ... systemctl start`, and timeout execs the binary directly, so a
    shell function would be bypassed entirely.

    Failed units live in a state file so `start` can clear one and have a later
    `is-failed` observe the recovery across process boundaries.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    failed_file = tmp_path / "failed-units"
    failed_file.write_text(
        "".join(f"{u}\n" for u in failed_units.split() if u), encoding="utf-8"
    )

    stub = bin_dir / "systemctl"
    stub.write_text(
        """#!/bin/bash
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
case "$1" in
    is-failed)
        grep -qxF "$3" "$FAILED_FILE" 2>/dev/null && exit 0 || exit 1
        ;;
    start)
        if [[ "${START_RC:-0}" == "0" ]]; then
            grep -vxF "$2" "$FAILED_FILE" > "$FAILED_FILE.tmp" 2>/dev/null || true
            mv -f "$FAILED_FILE.tmp" "$FAILED_FILE" 2>/dev/null || true
            exit 0
        fi
        exit "$START_RC"
        ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    script = f"""
source {shlex.quote(str(ROOT / 'lib' / 'utils.sh'))}
{snippet}
"""
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(log_path),
        "FAILED_FILE": str(failed_file),
        "START_RC": str(start_rc),
    }
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_recover_failed_units_ignores_healthy_units(tmp_path: Path) -> None:
    """Healthy units must never be reset or restarted.

    This is the safety property that separates recovery from a blind
    reset-failed sweep on every deploy.
    """
    log_path = tmp_path / "systemctl.log"
    log_path.touch()

    result = run_recover_snippet(
        "homelab_recover_failed_units homelab-docker-update.service",
        log_path,
        tmp_path,
        failed_units="",
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "reset-failed" not in log
    assert "start" not in log


def test_recover_failed_units_resets_then_starts(tmp_path: Path) -> None:
    """reset-failed must precede start: it clears the StartLimitBurst limiter
    that would otherwise make systemd refuse the start outright."""
    log_path = tmp_path / "systemctl.log"

    result = run_recover_snippet(
        "homelab_recover_failed_units homelab-docker-update.service",
        log_path,
        tmp_path,
        failed_units="homelab-docker-update.service",
    )

    assert result.returncode == 0, result.stderr
    log = log_path.read_text(encoding="utf-8")
    assert "reset-failed homelab-docker-update.service" in log
    assert log.index("reset-failed") < log.index("start homelab-docker-update.service")
    assert "recovered" in result.stdout


def test_recover_failed_units_leaves_persistent_failure_visible(tmp_path: Path) -> None:
    """A unit that fails again must stay failed so alerting still sees it,
    and must not abort the deploy."""
    log_path = tmp_path / "systemctl.log"

    result = run_recover_snippet(
        "homelab_recover_failed_units homelab-docker-update.service",
        log_path,
        tmp_path,
        failed_units="homelab-docker-update.service",
        start_rc=1,
    )

    assert result.returncode == 0, result.stderr
    # print_warn writes to stdout (lib/print.sh); accepting either stream would let a
    # regression that reroutes an operator-facing warning to stderr pass unnoticed.
    assert "still failing" in result.stdout, result.stderr


def test_mask_unwanted_service_skips_uninstalled_unit(tmp_path: Path) -> None:
    log_path = tmp_path / "systemctl.log"

    result = run_utils_snippet(
        "homelab_mask_unwanted_service openipmi.service",
        log_path,
        {"SYSTEMCTL_UNIT_KNOWN": "0"},
    )

    assert result.returncode == 0, result.stderr
    assert "nothing to mask" in result.stdout
    assert "mask openipmi.service" not in log_path.read_text(encoding="utf-8")


def _stage_ssh_config_installer(tmp_path: Path) -> tuple[Path, Path]:
    """Lay out the staged bundle the way the deploy does, minus the SSH."""
    module_dir = tmp_path / "ssh-config"
    scripts_dir = module_dir / "scripts"
    build_dir = module_dir / "build" / "ace"
    scripts_dir.mkdir(parents=True)
    build_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "ssh-config" / "scripts" / "install.py", scripts_dir / "install.py")
    (build_dir / "config").write_text("Host ace\n", encoding="utf-8")
    return module_dir, scripts_dir


def _run_ssh_config_installer(scripts_dir: Path, module_dir: Path, home: Path):
    child_env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "lib" / "py"),
    }
    # Do not instrument the installer subprocess.
    #
    # pytest-cov ships a `pytest-cov.pth` that starts coverage in any Python
    # child when COV_CORE_SOURCE is set, which it sets for the whole session.
    # The child reads branch mode from COV_CORE_BRANCH and enables it only for
    # the literal string "enabled" (`pytest_cov/embed.py`), so the child writes a
    # *statement-only* data file beside the parent's branch-mode one. pytest-cov
    # runs with `data_suffix=True` and combines every `.coverage.*` at the end of
    # the session, and that combine then fails outright with "Can't combine
    # statement coverage data with branch data" -- after every test has passed.
    #
    # The older subprocess tests in this file never hit it because they spawn
    # `bash`. Stripping the whole COV_CORE_* family is deliberate: the exact
    # variables have changed across pytest-cov majors, and this repo's pinned
    # version differs from the one CI resolves.
    for name in [key for key in child_env if key.startswith("COV_CORE")]:
        child_env.pop(name, None)
    for name in ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG"):
        child_env.pop(name, None)

    return subprocess.run(
        [sys.executable, str(scripts_dir / "install.py"), "ace"],
        cwd=module_dir,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ssh_config_installer_propagates_copy_failure(tmp_path: Path) -> None:
    """A write that fails must not be reported as a successful deploy.

    The bash version returned 2 from `backup_and_copy_if_changed` and the script
    propagated it. The Python `run()` harness maps an unexpected exception to the
    same exit code, so the guarantee is unchanged -- here the failure is real
    rather than stubbed: `~/.ssh` is occupied by a regular file, so creating the
    directory raises.
    """
    module_dir, scripts_dir = _stage_ssh_config_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".ssh").write_text("not a directory\n", encoding="utf-8")

    result = _run_ssh_config_installer(scripts_dir, module_dir, home)

    assert result.returncode == 2, result.stdout + result.stderr


def test_ssh_config_installer_runs_without_root(tmp_path: Path) -> None:
    """Its target is the deploy user's own home. `run(require_root=False)` is what
    keeps it from refusing -- and the test suite does not run as root, so a
    regression that made it root-only would fail here immediately."""
    module_dir, scripts_dir = _stage_ssh_config_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run_ssh_config_installer(scripts_dir, module_dir, home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / ".ssh" / "config").read_text(encoding="utf-8") == "Host ace\n"


def test_ssh_config_installer_locks_down_the_ssh_directory(tmp_path: Path) -> None:
    """ssh ignores a config in a directory it considers too open, so a deploy
    that left ~/.ssh at the default umask would silently change nothing."""
    module_dir, scripts_dir = _stage_ssh_config_installer(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    _run_ssh_config_installer(scripts_dir, module_dir, home)

    assert (home / ".ssh").stat().st_mode & 0o777 == 0o700
    assert (home / ".ssh" / "config").stat().st_mode & 0o777 == 0o600


def test_ssh_config_installer_backs_up_the_config_it_replaces(tmp_path: Path) -> None:
    """This is the file that, wrong, locks you out of the host you would fix it
    from -- so the previous one has to survive on the host itself."""
    module_dir, scripts_dir = _stage_ssh_config_installer(tmp_path)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host old\n", encoding="utf-8")

    _run_ssh_config_installer(scripts_dir, module_dir, home)

    backups = list((home / ".ssh").glob("config.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "Host old\n"


def test_keepalived_build_uses_caller_supplied_tmpfs_path(
    offline: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # `offline` is load-bearing, not decoration: normalize_config resolves the
    # keepalived env path eagerly to build its error messages, so without it this
    # test shells out to the `op` CLI and fails on any machine without a session.
    values = {
        "config.type": "ubuntu",
        "keepalived.interface": "eth0",
        "keepalived.instance_name": "test",
        "keepalived.healthcheck_host_env": "HEALTHCHECK_HOST",
        "keepalived.healthcheck_url_env": "HEALTHCHECK_URL",
        "keepalived.unicast_src_ip": "10.0.0.10",
        "keepalived.virtual_router_id": 10,
        "keepalived.priority": 100,
        "keepalived.advert_interval": 1,
        "keepalived.preempt_delay": 0,
        "keepalived.unicast_peers": ["10.0.0.11"],
        "keepalived.virtual_ips": ["10.0.0.15/24"],
    }

    class Registry:
        def get(self, _host: str, key: str, default: object = None) -> object:
            return values.get(key, default)

    monkeypatch.setattr(keepalived, "default_registry", lambda _root: Registry())
    monkeypatch.setattr(
        keepalived,
        "load_keepalived_env",
        lambda _root: {
            "HEALTHCHECK_HOST": "route.example.net",
            "HEALTHCHECK_URL": "https://route.example.net/ping",
        },
    )
    build_dir = tmp_path / "tmpfs-stage"

    artifacts = keepalived.build_host_artifacts(ROOT, "safety-test", build_dir)

    assert artifacts.build_dir == build_dir
    assert "route.example.net" in (build_dir / "healthcheck.sh").read_text(encoding="utf-8")
    assert not (ROOT / "keepalived" / "build" / "safety-test").exists()


def test_keepalived_stages_the_python_installer_under_python3(
    offline: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """keepalived is the first module ported off bash (freender/homelab-ops#36), and
    it reaches the target through `stage_and_run_remote_installer` directly rather
    than `simple_root_installer_deploy`.

    Two things have to hold together or the deploy fails as root on the host: the
    installer name must be the `.py` one that exists in the bundle, and `interpreter`
    must be `python3`. The suffix is also what makes the staging step upload
    `lib/py/` and set `PYTHONPATH`, so a name reverted to `install.sh` would take the
    library with it silently.

    Asserted here rather than in a keepalived-specific file because this is the
    live-deploy branch of a network-critical module — `test_dry_run_all_modules.py`
    returns before it, so nothing else in the suite reaches it.
    """
    staged: dict[str, object] = {}

    def record(*args: object, **kwargs: object) -> None:
        staged["args"] = args
        staged["kwargs"] = kwargs

    monkeypatch.setattr(keepalived, "stage_and_run_remote_installer", record)
    monkeypatch.setattr(keepalived, "HostConnection", lambda *a, **kw: object())
    monkeypatch.setattr(keepalived, "diff_many", lambda *a, **kw: [])
    monkeypatch.setattr(keepalived, "build_host_artifacts", lambda root, host, build_dir: (
        keepalived.HostArtifacts(build_dir=build_dir, file_specs=[])
    ))

    class Registry:
        def get(self, _host: str, key: str, default: object = None) -> object:
            return {"config.user": "root", "config.hostname": "neo.internal"}.get(key, default)

    monkeypatch.setattr(keepalived, "default_registry", lambda _root: Registry())

    keepalived.deploy_host(ROOT, "neo", dry_run=False, force=False)

    assert staged["args"][4] == "scripts/install.py"
    assert staged["kwargs"]["interpreter"] == "python3"
    # The installer asserts geteuid() == 0 itself; the bash `sudo -n` re-exec it
    # replaced was dead code on all three root-user targets (#31).
    assert staged["kwargs"]["require_root"] is False


def test_keepalived_ships_exactly_one_installer() -> None:
    """The migration rule: `install.py` added and `install.sh` deleted in the same
    commit, never both present. Both present would leave which one runs decided by
    `keepalived.py` alone, with a stale copy of the logic beside it."""
    scripts = ROOT / "keepalived" / "scripts"

    assert (scripts / "install.py").is_file()
    assert not (scripts / "install.sh").exists()
    assert sorted(path.name for path in scripts.glob("install*")) == ["install.py"]
