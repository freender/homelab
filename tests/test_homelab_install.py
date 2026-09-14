"""Tests for `lib/py/homelab_install`, the shared remote-installer library.

The prototype (freender/homelab-ops#33) was verified by a throwaway script that was
never committed. This file is that verification made permanent, and it is what makes
adding `homelab_install` to `[tool.coverage.run] source` in #34 honest rather than a
way to book 0% as covered.

Imported in-process, which #31 decision 4 permits in this direction only —
`tests/` may import `homelab_install`, never the reverse. `tests/conftest.py` puts
`lib/py` on `sys.path`; nothing here goes through SSH, a real `/etc`, a real
`systemctl`, or a real `apt-get`.

The assertions are deliberately exact on two things the bash original got wrong or
left implicit, because a looser assertion passes the mutant that matters:

* **Change tracking.** `install.sh`'s tri-state `rc=` dance is what the `ChangeSet`
  replaces, and `systemd.ensure_running(changed=...)` is the only consumer. A file
  install that silently stopped recording would leave a changed `keepalived.conf` on
  disk with the daemon never restarted — a live config and a stale process, which is
  exactly the failure mode a VIP module must not have.
* **Idempotency.** The second run of an installer must write nothing and restart
  nothing. `./deploy keepalived all` runs on every `/ship`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from homelab_install import files, log, main, packages, systemd
from homelab_install.changes import ChangeSet
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ctx(tmp_path: Path, *, force: bool = False, **file_map: tuple[str, str]) -> InstallContext:
    build_dir = tmp_path / "build" / "testhost"
    build_dir.mkdir(parents=True, exist_ok=True)
    return InstallContext(
        host="testhost",
        script_dir=tmp_path,
        build_dir=build_dir,
        env={},
        file_map=dict(file_map),
        force_update=force,
    )


class FakeRun:
    """Records `subprocess.run` calls and replays canned return codes."""

    def __init__(self, codes: dict[tuple[str, ...], int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = codes or {}

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        code = self.codes.get(tuple(command), 0)
        if code != 0 and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code)


# ---------------------------------------------------------------------------
# ChangeSet
# ---------------------------------------------------------------------------


def test_change_set_starts_empty() -> None:
    assert ChangeSet().any() is False


def test_change_set_records_and_reports_by_name() -> None:
    changes = ChangeSet()
    changes.record("keepalived.conf")

    assert changes.any() is True
    assert changes.touched("keepalived.conf") is True
    assert changes.touched("healthcheck.sh") is False
    assert changes.touched("healthcheck.sh", "keepalived.conf") is True


def test_change_set_deduplicates_and_sorts_names() -> None:
    changes = ChangeSet()
    changes.record("b")
    changes.record("a")
    changes.record("b")

    assert changes.names() == ("a", "b")


def test_change_sets_do_not_share_state_between_contexts(tmp_path: Path) -> None:
    """`field(default_factory=...)` rather than a shared default — a mutable class
    attribute here would leak one host's changes into the next host's run."""
    first = _ctx(tmp_path / "a")
    second = _ctx(tmp_path / "b")

    first.changes.record("x")

    assert second.changes.any() is False


# ---------------------------------------------------------------------------
# files.install / files.install_all
# ---------------------------------------------------------------------------


def test_install_writes_a_new_file_with_its_mode_and_records_the_change(tmp_path: Path) -> None:
    dest = tmp_path / "etc" / "keepalived" / "keepalived.conf"
    ctx = _ctx(tmp_path, **{"keepalived.conf": (str(dest), "640")})
    (ctx.build_dir / "keepalived.conf").write_text("vrrp\n", encoding="utf-8")

    assert files.install(ctx, "keepalived.conf") is True
    assert dest.read_text(encoding="utf-8") == "vrrp\n"
    assert dest.stat().st_mode & 0o777 == 0o640
    assert ctx.changes.touched("keepalived.conf")


def test_install_creates_missing_parent_directories(tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "nested" / "path" / "file.conf"
    ctx = _ctx(tmp_path, **{"file.conf": (str(dest), "644")})
    (ctx.build_dir / "file.conf").write_text("x\n", encoding="utf-8")

    files.install(ctx, "file.conf")

    assert dest.is_file()


def test_install_is_a_no_op_when_content_already_matches(tmp_path: Path) -> None:
    dest = tmp_path / "target.conf"
    dest.write_text("same\n", encoding="utf-8")
    ctx = _ctx(tmp_path, **{"target.conf": (str(dest), "644")})
    (ctx.build_dir / "target.conf").write_text("same\n", encoding="utf-8")

    assert files.install(ctx, "target.conf") is False
    assert ctx.changes.any() is False


def test_install_still_corrects_the_mode_of_an_unchanged_file(tmp_path: Path) -> None:
    """Content and permissions are separate; the bash original repaired the mode on
    the unchanged path too, and a config left world-readable is a real finding."""
    dest = tmp_path / "target.conf"
    dest.write_text("same\n", encoding="utf-8")
    dest.chmod(0o666)
    ctx = _ctx(tmp_path, **{"target.conf": (str(dest), "600")})
    (ctx.build_dir / "target.conf").write_text("same\n", encoding="utf-8")

    files.install(ctx, "target.conf")

    assert dest.stat().st_mode & 0o777 == 0o600
    assert ctx.changes.any() is False


def test_install_rewrites_an_unchanged_file_under_force(tmp_path: Path) -> None:
    dest = tmp_path / "target.conf"
    dest.write_text("same\n", encoding="utf-8")
    ctx = _ctx(tmp_path, force=True, **{"target.conf": (str(dest), "644")})
    (ctx.build_dir / "target.conf").write_text("same\n", encoding="utf-8")

    assert files.install(ctx, "target.conf") is True
    assert ctx.changes.touched("target.conf")


def test_install_overwrites_differing_content(tmp_path: Path) -> None:
    dest = tmp_path / "target.conf"
    dest.write_text("old\n", encoding="utf-8")
    ctx = _ctx(tmp_path, **{"target.conf": (str(dest), "644")})
    (ctx.build_dir / "target.conf").write_text("new\n", encoding="utf-8")

    assert files.install(ctx, "target.conf") is True
    assert dest.read_text(encoding="utf-8") == "new\n"


def test_install_raises_on_an_unknown_file_map_entry(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    with pytest.raises(InstallError, match="missing file-map entry: nope"):
        files.install(ctx, "nope")


def test_install_raises_when_the_build_file_is_absent(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, **{"ghost.conf": (str(tmp_path / "out.conf"), "644")})

    with pytest.raises(InstallError, match="missing build file"):
        files.install(ctx, "ghost.conf")


def test_install_all_installs_every_entry_and_reports_any_change(tmp_path: Path) -> None:
    first = tmp_path / "one.conf"
    second = tmp_path / "two.conf"
    ctx = _ctx(
        tmp_path,
        **{"one.conf": (str(first), "644"), "two.conf": (str(second), "755")},
    )
    (ctx.build_dir / "one.conf").write_text("1\n", encoding="utf-8")
    (ctx.build_dir / "two.conf").write_text("2\n", encoding="utf-8")

    assert files.install_all(ctx) is True
    assert first.is_file() and second.is_file()
    assert ctx.changes.names() == ("one.conf", "two.conf")


def test_install_all_returns_false_when_nothing_changed(tmp_path: Path) -> None:
    """The value `systemd.ensure_running(changed=...)` is driven from. A version
    that returned True unconditionally would restart keepalived on every deploy."""
    dest = tmp_path / "one.conf"
    dest.write_text("1\n", encoding="utf-8")
    ctx = _ctx(tmp_path, **{"one.conf": (str(dest), "644")})
    (ctx.build_dir / "one.conf").write_text("1\n", encoding="utf-8")

    assert files.install_all(ctx) is False


def test_install_all_reports_a_change_even_when_only_one_entry_moved(tmp_path: Path) -> None:
    unchanged = tmp_path / "one.conf"
    unchanged.write_text("1\n", encoding="utf-8")
    changed = tmp_path / "two.conf"
    ctx = _ctx(
        tmp_path,
        **{"one.conf": (str(unchanged), "644"), "two.conf": (str(changed), "644")},
    )
    (ctx.build_dir / "one.conf").write_text("1\n", encoding="utf-8")
    (ctx.build_dir / "two.conf").write_text("2\n", encoding="utf-8")

    assert files.install_all(ctx) is True
    assert ctx.changes.names() == ("two.conf",)


def test_install_all_honours_exclude(tmp_path: Path) -> None:
    skipped = tmp_path / "skip.conf"
    ctx = _ctx(
        tmp_path,
        **{"skip.conf": (str(skipped), "644"), "keep.conf": (str(tmp_path / "keep.conf"), "644")},
    )
    (ctx.build_dir / "skip.conf").write_text("s\n", encoding="utf-8")
    (ctx.build_dir / "keep.conf").write_text("k\n", encoding="utf-8")

    files.install_all(ctx, exclude=("skip.conf",))

    assert not skipped.exists()
    assert ctx.changes.names() == ("keep.conf",)


# ---------------------------------------------------------------------------
# packages.ensure
# ---------------------------------------------------------------------------


def test_ensure_skips_apt_when_the_probe_is_already_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    monkeypatch.setattr(packages, "_run", fake)
    monkeypatch.setattr(packages.shutil, "which", lambda name: "/usr/sbin/keepalived")

    packages.ensure(_ctx(tmp_path), "keepalived", "curl", probe="keepalived")

    assert fake.calls == []


def test_ensure_installs_when_the_probe_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    monkeypatch.setattr(packages, "_run", fake)
    monkeypatch.setattr(packages.shutil, "which", lambda name: None)

    packages.ensure(_ctx(tmp_path), "keepalived", "curl", probe="keepalived")

    assert fake.calls == [["apt-get", "install", "-y", "-q", "keepalived", "curl"]]


def test_ensure_without_a_probe_always_calls_apt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun()
    monkeypatch.setattr(packages, "_run", fake)
    monkeypatch.setattr(packages.shutil, "which", lambda name: "/usr/bin/anything")

    packages.ensure(_ctx(tmp_path), "curl")

    assert fake.calls == [["apt-get", "install", "-y", "-q", "curl"]]


def test_ensure_raises_install_error_when_apt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`InstallError`, not `CalledProcessError` — #31 requires a module to be able
    to catch a single failed item and continue, as docker-stacks does in bash."""
    fake = FakeRun({("apt-get", "install", "-y", "-q", "keepalived"): 100})
    monkeypatch.setattr(packages, "_run", fake)
    monkeypatch.setattr(packages.shutil, "which", lambda name: None)

    with pytest.raises(InstallError, match="failed to install packages: keepalived"):
        packages.ensure(_ctx(tmp_path), "keepalived", probe="keepalived")


# ---------------------------------------------------------------------------
# systemd.ensure_running — the enable / restart / start ladder
# ---------------------------------------------------------------------------


def _systemd_fake(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool, active: bool
) -> FakeRun:
    fake = FakeRun(
        {
            ("systemctl", "is-enabled", "--quiet", "keepalived"): 0 if enabled else 1,
            ("systemctl", "is-active", "--quiet", "keepalived"): 0 if active else 3,
        }
    )
    monkeypatch.setattr(systemd, "_run", fake)
    return fake


def test_ensure_running_enables_a_unit_that_is_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _systemd_fake(monkeypatch, enabled=False, active=False)

    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=False)

    assert ["systemctl", "enable", "--now", "keepalived"] in fake.calls
    assert ["systemctl", "restart", "keepalived"] not in fake.calls


def test_ensure_running_restarts_an_enabled_unit_whose_files_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _systemd_fake(monkeypatch, enabled=True, active=True)

    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=True)

    assert ["systemctl", "restart", "keepalived"] in fake.calls


def test_ensure_running_starts_an_enabled_but_dead_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _systemd_fake(monkeypatch, enabled=True, active=False)

    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=False)

    assert ["systemctl", "start", "keepalived"] in fake.calls
    assert ["systemctl", "restart", "keepalived"] not in fake.calls


def test_ensure_running_touches_nothing_when_enabled_active_and_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idempotent path. A redeploy that changed no file must not bounce the VIP."""
    fake = _systemd_fake(monkeypatch, enabled=True, active=True)

    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=False)

    mutating = [call for call in fake.calls if call[1] not in {"is-enabled", "is-active"}]
    assert mutating == []


def test_ensure_running_reloads_only_when_something_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _systemd_fake(monkeypatch, enabled=True, active=True)
    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=True)
    assert ["systemctl", "daemon-reload"] in fake.calls

    fake = _systemd_fake(monkeypatch, enabled=True, active=True)
    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=False)
    assert ["systemctl", "daemon-reload"] not in fake.calls


def test_ensure_running_reloads_before_restarting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _systemd_fake(monkeypatch, enabled=True, active=True)

    systemd.ensure_running(_ctx(tmp_path), "keepalived", changed=True)

    assert fake.calls.index(["systemctl", "daemon-reload"]) < fake.calls.index(
        ["systemctl", "restart", "keepalived"]
    )


# ---------------------------------------------------------------------------
# main._parse_file_map / main._parse_env_file
# ---------------------------------------------------------------------------


def test_parse_file_map_reads_name_dest_mode(tmp_path: Path) -> None:
    path = tmp_path / "file-map.conf"
    path.write_text(
        "keepalived.conf|/etc/keepalived/keepalived.conf|644\n"
        "healthcheck.sh|/etc/keepalived/healthcheck.sh|755\n",
        encoding="utf-8",
    )

    assert main._parse_file_map(path) == {
        "keepalived.conf": ("/etc/keepalived/keepalived.conf", "644"),
        "healthcheck.sh": ("/etc/keepalived/healthcheck.sh", "755"),
    }


def test_parse_file_map_defaults_a_missing_mode_to_644(tmp_path: Path) -> None:
    """Matches `load_file_map` in `lib/utils.sh`; a different default would silently
    re-permission every entry written without one."""
    path = tmp_path / "file-map.conf"
    path.write_text("a.conf|/etc/a.conf\nb.conf|/etc/b.conf|\n", encoding="utf-8")

    parsed = main._parse_file_map(path)

    assert parsed["a.conf"] == ("/etc/a.conf", "644")
    assert parsed["b.conf"] == ("/etc/b.conf", "644")


def test_parse_file_map_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "file-map.conf"
    path.write_text("a.conf|/etc/a.conf|644\n\n   \n", encoding="utf-8")

    assert list(main._parse_file_map(path)) == ["a.conf"]


def test_parse_file_map_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert main._parse_file_map(tmp_path / "absent") == {}


def test_parse_file_map_rejects_a_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "file-map.conf"
    path.write_text("no-separator-here\n", encoding="utf-8")

    with pytest.raises(InstallError, match="malformed file-map line"):
        main._parse_file_map(path)


def test_parse_env_file_round_trips_write_env_files_quoting(tmp_path: Path) -> None:
    """Parsed, not sourced. The format is `write_env_file`'s (`src/homelab/build.py`),
    and the value below is exactly the kind a `source` would execute."""
    import shlex

    path = tmp_path / "env"
    path.write_text(
        "\n".join(
            f"{key}={shlex.quote(value)}"
            for key, value in [
                ("PAUSED", "false"),
                ("MESSAGE", "hello world"),
                ("DANGEROUS", "$(rm -rf /)"),
                ("EMPTY", ""),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert main._parse_env_file(path) == {
        "PAUSED": "false",
        "MESSAGE": "hello world",
        "DANGEROUS": "$(rm -rf /)",
        "EMPTY": "",
    }


def test_parse_env_file_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert main._parse_env_file(tmp_path / "absent") == {}


def test_parse_env_file_skips_blank_lines(tmp_path: Path) -> None:
    """`write_env_file` separates sections with blank lines; treating one as a
    malformed assignment would fail every module that ships an env file."""
    path = tmp_path / "env"
    path.write_text("PAUSED=false\n\n   \nSCHEDULE=daily\n", encoding="utf-8")

    assert main._parse_env_file(path) == {"PAUSED": "false", "SCHEDULE": "daily"}


def test_parse_env_file_rejects_a_line_without_an_assignment(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text("PAUSED=false\nGARBAGE\n", encoding="utf-8")

    with pytest.raises(InstallError, match=r"malformed env line in .*:2"):
        main._parse_env_file(path)


# ---------------------------------------------------------------------------
# main.run — the harness
# ---------------------------------------------------------------------------


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    argv: list[str] | None = None,
    euid: int = 0,
    environ: dict[str, str] | None = None,
) -> None:
    script = tmp_path / "scripts" / "install.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.touch()
    monkeypatch.setattr(sys, "argv", [str(script), *(["testhost"] if argv is None else argv)])
    monkeypatch.setattr(main.os, "geteuid", lambda: euid)
    monkeypatch.setattr(main.os, "environ", environ if environ is not None else {})


def test_run_builds_the_context_from_the_build_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = tmp_path / "build" / "testhost"
    build.mkdir(parents=True)
    (build / "file-map.conf").write_text("a.conf|/etc/a.conf|600\n", encoding="utf-8")
    (build / "env").write_text("PAUSED=false\n", encoding="utf-8")
    _harness(monkeypatch, tmp_path)
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    ctx = seen[0]
    assert ctx.host == "testhost"
    assert ctx.build_dir == build
    assert ctx.file_map == {"a.conf": ("/etc/a.conf", "600")}
    assert ctx.env == {"PAUSED": "false"}
    assert ctx.force_update is False


def test_run_defaults_the_host_to_the_local_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _harness(monkeypatch, tmp_path, argv=[])
    monkeypatch.setattr(main.socket, "gethostname", lambda: "neo")
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    assert seen[0].host == "neo"


def test_run_reads_force_update_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not from `build/<host>/env`: `stage_and_run_remote_installer` passes it via
    `env=`, and `ctx.env` deliberately means the rendered file instead."""
    _harness(monkeypatch, tmp_path, environ={"FORCE_UPDATE": "true"})
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    assert seen[0].force_update is True


def test_run_accepts_force_as_a_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch, tmp_path, argv=["testhost", "--force"])
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    assert seen[0].force_update is True


def test_run_treats_a_missing_force_update_as_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _harness(monkeypatch, tmp_path, environ={"FORCE_UPDATE": "false"})
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    assert seen[0].force_update is False


def test_run_refuses_to_proceed_as_a_non_root_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _harness(monkeypatch, tmp_path, euid=1000)

    def explode(_ctx: InstallContext) -> None:
        raise AssertionError("must not run as a non-root user")

    with pytest.raises(SystemExit) as excinfo:
        main.run(explode, "Demo")

    assert excinfo.value.code == 1
    assert "must be run as root" in capsys.readouterr().err


def test_run_prints_the_module_footer_after_a_successful_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _harness(monkeypatch, tmp_path)

    main.run(lambda _ctx: None, "PVE Postinstall Webhook")

    assert capsys.readouterr().out.splitlines()[-1] == "=== PVE Postinstall Webhook Complete ==="


def test_run_exits_1_on_install_error_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _harness(monkeypatch, tmp_path)

    def fail(_ctx: InstallContext) -> None:
        raise InstallError("keepalived.conf is not valid")

    with pytest.raises(SystemExit) as excinfo:
        main.run(fail, "Demo")

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "✗ Error: keepalived.conf is not valid" in captured.err
    assert "Traceback" not in captured.err
    assert "Complete" not in captured.out


def test_run_exits_2_with_a_traceback_on_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A distinct exit code: 1 is "the installer said no", 2 is "the installer broke"."""
    _harness(monkeypatch, tmp_path)

    def fail(_ctx: InstallContext) -> None:
        raise RuntimeError("boom")

    with pytest.raises(SystemExit) as excinfo:
        main.run(fail, "Demo")

    assert excinfo.value.code == 2
    assert "Traceback" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# log — frozen byte-for-byte on lib/print.sh (#31 decision 5)
# ---------------------------------------------------------------------------


def test_log_helpers_match_print_sh_byte_for_byte(capsys: pytest.CaptureFixture[str]) -> None:
    log.header("Keepalived")
    log.action("Package")
    log.sub("detail")
    log.ok("done")
    log.warn("careful")
    log.error("broken")

    captured = capsys.readouterr()
    assert captured.out == (
        "=== Keepalived ===\n"
        "==> Package\n"
        "    detail\n"
        "    \u2713 done\n"
        "    \u2717 Warning: careful\n"
    )
    assert captured.err == "    \u2717 Error: broken\n"


def test_log_format_still_matches_the_bash_originals() -> None:
    """Read the real `lib/print.sh` rather than restating its format here, so the
    two cannot drift apart silently while both tests stay green."""
    print_sh = (REPO_ROOT / "lib" / "print.sh").read_text(encoding="utf-8")

    assert 'echo "=== $* ==="' in print_sh
    assert 'echo "==> $*"' in print_sh
    assert 'echo "    $*"' in print_sh
    assert 'echo "    \u2713 $*"' in print_sh
    assert 'echo "    \u2717 Warning: $*"' in print_sh
    assert 'echo "    \u2717 Error: $*" >&2' in print_sh


# ---------------------------------------------------------------------------
# Hermetic against the orchestrator (#31 decision 4)
# ---------------------------------------------------------------------------


def test_the_library_imports_nothing_outside_the_standard_library() -> None:
    """The hard rule: `homelab_install` runs on every target, where Fabric, click and
    rich do not exist. Checked on the source rather than by importing, so an import
    added inside a function body is caught too.
    """
    import ast

    package = REPO_ROOT / "lib" / "py" / "homelab_install"
    offenders: list[str] = []

    for source_file in sorted(package.glob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0 and module.split(".")[0] not in _STDLIB:
                    offenders.append(f"{source_file.name}: from {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in _STDLIB:
                        offenders.append(f"{source_file.name}: import {alias.name}")

    assert offenders == []


_STDLIB = sys.stdlib_module_names
