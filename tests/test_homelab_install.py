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

from homelab_install import env, files, log, main, packages, systemd
from homelab_install.changes import ChangeSet
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ctx(
    tmp_path: Path,
    *,
    force: bool = False,
    deploy_env: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    **file_map: tuple[str, str],
) -> InstallContext:
    build_dir = tmp_path / "build" / "testhost"
    build_dir.mkdir(parents=True, exist_ok=True)
    return InstallContext(
        host="testhost",
        script_dir=tmp_path,
        build_dir=build_dir,
        env=dict(env or {}),
        deploy_env=dict(deploy_env or {}),
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


class FakeApt:
    """A stateful `dpkg-query`/`apt-get` double.

    Stateful on purpose: `packages.ensure` checks, installs, then checks *again*,
    and a fake replaying canned codes would answer the re-verify pass with the
    same "missing" it gave the first pass. That would make the re-verify branch
    untestable — and re-verifying is the one thing `base-packages`' bash did that
    a naive port would drop.

    `known` maps package -> dpkg `Status` field. A package absent from it is one
    `dpkg-query` exits 1 for.
    """

    INSTALLED = "install ok installed"

    def __init__(
        self,
        known: dict[str, str] | None = None,
        *,
        installs: bool = True,
        update_code: int = 0,
        install_code: int = 0,
    ) -> None:
        self.known = dict(known or {})
        self.installs = installs
        self.update_code = update_code
        self.install_code = install_code
        self.calls: list[list[str]] = []
        self.env: list[dict[str, str] | None] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        self.env.append(kwargs.get("env"))

        if command[0] == "dpkg-query":
            package = command[-1]
            if package not in self.known:
                return subprocess.CompletedProcess(command, 1, stdout="")
            return subprocess.CompletedProcess(command, 0, stdout=self.known[package])

        if command[:2] == ["apt-get", "update"]:
            return subprocess.CompletedProcess(command, self.update_code)

        if command[:2] == ["apt-get", "install"]:
            if self.installs and self.install_code == 0:
                for package in command[4:]:
                    self.known[package] = self.INSTALLED
            return subprocess.CompletedProcess(command, self.install_code)

        raise AssertionError(f"unexpected command: {command}")

    @property
    def apt_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "apt-get"]


def _apt(monkeypatch: pytest.MonkeyPatch, fake: FakeApt) -> FakeApt:
    monkeypatch.setattr(packages, "_run", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_apt_update_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`packages._apt_updated` coalesces `apt-get update` across one installer
    process. Without this reset the first test to install would suppress the
    update in every test after it, and the ordering assertions would pass for
    the wrong reason."""
    monkeypatch.setattr(packages, "_apt_updated", False)


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


def test_ensure_touches_apt_not_at_all_when_every_package_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-op run must not hit the network. `base-packages` is first in
    `MODULE_ORDER` and runs on every host on every deploy; an unconditional
    `apt-get update` there would put a network round-trip in front of
    everything else the repo does."""
    fake = _apt(monkeypatch, FakeApt({"mbuffer": FakeApt.INSTALLED, "vim": FakeApt.INSTALLED}))

    packages.ensure(_ctx(tmp_path), "mbuffer", "vim")

    assert fake.apt_calls == []


def test_ensure_installs_only_the_packages_that_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug the `probe=` removal fixed, pinned as a regression.

    `ensure(ctx, "keepalived", "curl", probe="keepalived")` skipped *both* when
    the keepalived binary was on PATH, so a host missing `curl` stayed missing it
    — and `curl` is what keepalived's `healthcheck.sh` runs to decide the VIP.
    """
    fake = _apt(monkeypatch, FakeApt({"keepalived": FakeApt.INSTALLED}))

    packages.ensure(_ctx(tmp_path), "keepalived", "curl")

    assert ["apt-get", "install", "-y", "-q", "curl"] in fake.calls


def test_ensure_runs_apt_get_update_before_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _apt(monkeypatch, FakeApt())

    packages.ensure(_ctx(tmp_path), "ripgrep")

    assert fake.apt_calls == [
        ["apt-get", "update", "-qq"],
        ["apt-get", "install", "-y", "-q", "ripgrep"],
    ]


def test_apt_get_update_runs_at_most_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The design doc's "one lazy `apt-get update` per run", which `base-packages`
    is the first module to need."""
    fake = _apt(monkeypatch, FakeApt())
    ctx = _ctx(tmp_path)

    packages.ensure(ctx, "mbuffer")
    packages.ensure(ctx, "ripgrep")

    assert fake.calls.count(["apt-get", "update", "-qq"]) == 1


def test_ensure_sets_debian_frontend_without_dropping_the_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing the environment rather than extending it would strip PATH, and
    apt-get shells out to maintainer-script helpers that need it."""
    monkeypatch.setenv("PATH", "/sentinel/bin")
    fake = _apt(monkeypatch, FakeApt())

    packages.ensure(_ctx(tmp_path), "mc")

    install_env = fake.env[fake.calls.index(["apt-get", "install", "-y", "-q", "mc"])]
    assert install_env["DEBIAN_FRONTEND"] == "noninteractive"
    assert install_env["PATH"] == "/sentinel/bin"


def test_ensure_raises_install_error_when_apt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`InstallError`, not `CalledProcessError` — #31 requires a module to be able
    to catch a single failed item and continue, as docker-stacks does in bash."""
    _apt(monkeypatch, FakeApt(install_code=100))

    with pytest.raises(InstallError, match="failed to install packages: keepalived"):
        packages.ensure(_ctx(tmp_path), "keepalived")


def test_ensure_raises_when_apt_get_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _apt(monkeypatch, FakeApt(update_code=1))

    with pytest.raises(InstallError, match="apt-get update failed"):
        packages.ensure(_ctx(tmp_path), "mbuffer")

    assert ["apt-get", "install", "-y", "-q", "mbuffer"] not in fake.calls


def test_ensure_raises_when_a_package_is_still_missing_after_a_successful_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-verify pass `base-packages`' bash did deliberately: a package that
    resolves but fails to configure leaves apt exiting 0, so the exit status alone
    would report it installed."""
    _apt(monkeypatch, FakeApt(installs=False))

    with pytest.raises(InstallError, match="packages still missing after install: mbuffer"):
        packages.ensure(_ctx(tmp_path), "mbuffer")


def test_ensure_re_verifies_only_what_it_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _apt(monkeypatch, FakeApt({"vim": FakeApt.INSTALLED}))

    packages.ensure(_ctx(tmp_path), "vim", "mc")

    queries = [call[-1] for call in fake.calls if call[0] == "dpkg-query"]
    assert queries == ["vim", "mc", "mc"]


# --- dpkg status parsing ---------------------------------------------------


def test_a_removed_but_not_purged_package_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dpkg -s` — what the bash used — exits 0 for a package in `config-files`
    state, so it reported a removed package as installed and never reinstalled
    it. Only the third word of the Status field answers the question."""
    fake = _apt(monkeypatch, FakeApt({"ripgrep": "deinstall ok config-files"}))

    packages.ensure(_ctx(tmp_path), "ripgrep")

    assert ["apt-get", "install", "-y", "-q", "ripgrep"] in fake.calls


def test_a_held_package_counts_as_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: matching the whole field against `install ok installed`
    would reinstall a held package on every single deploy. `apt-upgrade`
    dist-upgrades these hosts daily, so holds are a live condition here."""
    fake = _apt(monkeypatch, FakeApt({"mbuffer": "hold ok installed"}))

    packages.ensure(_ctx(tmp_path), "mbuffer")

    assert fake.apt_calls == []


def test_an_unknown_package_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _apt(monkeypatch, FakeApt())

    packages.ensure(_ctx(tmp_path), "mc")

    assert ["apt-get", "install", "-y", "-q", "mc"] in fake.calls


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


def test_run_keeps_the_deploy_env_and_the_build_env_file_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction `InstallContext` exists to enforce.

    `ctx.env` is what the orchestrator *rendered*; `ctx.deploy_env` is what it
    passed on the remote command line. `base-packages` has no build directory at
    all, so `BASE_PACKAGES` can only arrive by the second route — and a module
    that read the wrong one would silently get `{}` and refuse to run, or worse,
    inherit whatever the calling shell happened to hold.
    """
    build = tmp_path / "build" / "testhost"
    build.mkdir(parents=True)
    (build / "env").write_text("RENDERED=from-file\n", encoding="utf-8")
    _harness(monkeypatch, tmp_path, environ={"BASE_PACKAGES": "mbuffer vim mc ripgrep"})
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    assert seen[0].env == {"RENDERED": "from-file"}
    assert seen[0].deploy_env == {"BASE_PACKAGES": "mbuffer vim mc ripgrep"}


def test_deploy_env_is_empty_rather_than_absent_when_nothing_was_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _harness(monkeypatch, tmp_path)
    seen: list[InstallContext] = []

    main.run(seen.append, "Demo")

    assert seen[0].deploy_env == {}


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


# ---------------------------------------------------------------------------
# env — the build/<host>/env file (freender/homelab-ops#30, apt-upgrade)
#
# `_parse_env_file` has existed since #33 but nothing read its output until
# apt-upgrade. These pin the two things the bash got wrong in the same place:
# absent and empty were the same error, and an unparseable flag was `false`.
# ---------------------------------------------------------------------------


def test_require_accepts_an_env_file_that_has_every_name(tmp_path: Path) -> None:
    env.require(_ctx(tmp_path, env={"AUTOUPGRADE": "true", "PAUSED": "false"}), "AUTOUPGRADE")


def test_require_names_the_keys_that_are_absent(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, env={"AUTOUPGRADE": "true"})

    with pytest.raises(InstallError, match="missing: PAUSED, AUTO_REBOOT"):
        env.require(ctx, "AUTOUPGRADE", "PAUSED", "AUTO_REBOOT")


def test_require_separates_empty_from_absent(tmp_path: Path) -> None:
    """Different causes: an empty value is a bad `hosts.conf` entry, an absent one
    is a failed render. `require_env` in bash reported both as the same thing."""
    ctx = _ctx(tmp_path, env={"PAUSED": "   "})

    with pytest.raises(InstallError) as excinfo:
        env.require(ctx, "PAUSED", "AUTOUPGRADE")

    assert "empty: PAUSED" in str(excinfo.value)
    assert "missing: AUTOUPGRADE" in str(excinfo.value)


def test_require_points_at_the_env_file_it_read(tmp_path: Path) -> None:
    """The locator is the useful half of the message -- which host's build dir."""
    ctx = _ctx(tmp_path, env={})

    with pytest.raises(InstallError, match=r"build/testhost/env"):
        env.require(ctx, "PAUSED")


@pytest.mark.parametrize("raw", ["true", "TRUE", "True", "yes", "1", "on"])
def test_flag_accepts_every_true_spelling_normalize_bool_does(tmp_path: Path, raw: str) -> None:
    assert env.flag(_ctx(tmp_path, env={"AUTOUPGRADE": raw}), "AUTOUPGRADE") is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "no", "0", "off"])
def test_flag_accepts_every_false_spelling_normalize_bool_does(tmp_path: Path, raw: str) -> None:
    assert env.flag(_ctx(tmp_path, env={"AUTOUPGRADE": raw}), "AUTOUPGRADE") is False


def test_flag_raises_on_a_typo_rather_than_silently_disabling_the_feature(tmp_path: Path) -> None:
    """`[[ "$AUTOUPGRADE" == "true" ]]` mapped `ture` to false and turned the
    timer off with a fully successful deploy. This is the bug that motivated it."""
    ctx = _ctx(tmp_path, env={"AUTOUPGRADE": "ture"})

    with pytest.raises(InstallError, match="must be true or false"):
        env.flag(ctx, "AUTOUPGRADE")


def test_flag_falls_back_to_the_default_when_absent_or_empty(tmp_path: Path) -> None:
    assert env.flag(_ctx(tmp_path, env={}), "AUTO_REBOOT") is False
    assert env.flag(_ctx(tmp_path, env={}), "AUTO_REBOOT", default=True) is True
    assert env.flag(_ctx(tmp_path, env={"AUTO_REBOOT": ""}), "AUTO_REBOOT", default=True) is True


def test_text_returns_the_value_and_falls_back_when_blank(tmp_path: Path) -> None:
    assert env.text(_ctx(tmp_path, env={"SCHEDULE": "*-*-* 04:00:00"}), "SCHEDULE", "x") == (
        "*-*-* 04:00:00"
    )
    assert env.text(_ctx(tmp_path, env={"SCHEDULE": "  "}), "SCHEDULE", "fallback") == "fallback"
    assert env.text(_ctx(tmp_path, env={}), "SCHEDULE", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# files.remove — reversibility of a flag whose map entry is already gone
# ---------------------------------------------------------------------------


def test_remove_deletes_the_file_and_records_the_change(tmp_path: Path) -> None:
    target = tmp_path / "53homelab-auto-reboot"
    target.write_text("x\n", encoding="utf-8")
    ctx = _ctx(tmp_path)

    assert files.remove(ctx, str(target), reason="auto_reboot disabled") is True
    assert not target.exists()
    assert ctx.changes.any()


def test_remove_is_a_no_op_when_the_file_is_already_gone(tmp_path: Path) -> None:
    """Runs on every deploy to every host; "nothing to remove" is the normal case."""
    ctx = _ctx(tmp_path)

    assert files.remove(ctx, str(tmp_path / "absent")) is False
    assert not ctx.changes.any()


# ---------------------------------------------------------------------------
# systemd.pause / retire_unit / run_once / daemon_reload
# ---------------------------------------------------------------------------


def _units(monkeypatch: pytest.MonkeyPatch, **codes: int) -> FakeRun:
    fake = FakeRun({tuple(key.split("__")): value for key, value in codes.items()})
    monkeypatch.setattr(systemd, "_run", fake)
    return fake


def test_pause_stops_and_disables_a_running_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _units(
        monkeypatch,
        **{
            "systemctl__is-active__--quiet__t.timer": 0,
            "systemctl__is-enabled__--quiet__t.timer": 0,
        },
    )

    systemd.pause(_ctx(tmp_path), "t.timer")

    assert ["systemctl", "disable", "--now", "t.timer"] in fake.calls


def test_pause_leaves_the_unit_file_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pause is reversible; removing the file is retirement, and a resume would
    then have nothing to re-enable."""
    fake = _units(monkeypatch, **{"systemctl__is-active__--quiet__t.timer": 0})

    systemd.pause(_ctx(tmp_path), "t.timer")

    assert not any("rm" in call[0] for call in fake.calls)
    assert not any(call[:2] == ["systemctl", "daemon-reload"] for call in fake.calls)


def test_pause_does_not_disable_a_unit_that_is_already_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _units(
        monkeypatch,
        **{
            "systemctl__is-active__--quiet__t.timer": 3,
            "systemctl__is-enabled__--quiet__t.timer": 1,
        },
    )

    systemd.pause(_ctx(tmp_path), "t.timer")

    assert ["systemctl", "disable", "--now", "t.timer"] not in fake.calls


def test_pause_handles_several_units(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _units(
        monkeypatch,
        **{
            "systemctl__is-active__--quiet__a.timer": 0,
            "systemctl__is-active__--quiet__b.service": 0,
        },
    )

    systemd.pause(_ctx(tmp_path), "a.timer", "b.service")

    assert ["systemctl", "disable", "--now", "a.timer"] in fake.calls
    assert ["systemctl", "disable", "--now", "b.service"] in fake.calls


def test_retire_unit_disables_removes_and_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_path = tmp_path / "old.timer"
    unit_path.write_text("[Unit]\n", encoding="utf-8")
    fake = _units(monkeypatch, **{"systemctl__is-enabled__--quiet__old.timer": 0})

    assert systemd.retire_unit(_ctx(tmp_path), "old.timer", str(unit_path)) is True
    assert not unit_path.exists()
    assert ["systemctl", "disable", "--now", "old.timer"] in fake.calls
    assert ["systemctl", "daemon-reload"] in fake.calls


def test_retire_unit_reports_no_change_when_there_was_nothing_to_retire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal case on a host that never had the timer: no reload, no noise."""
    fake = _units(
        monkeypatch,
        **{
            "systemctl__is-enabled__--quiet__old.timer": 1,
            "systemctl__is-active__--quiet__old.timer": 3,
        },
    )

    assert systemd.retire_unit(_ctx(tmp_path), "old.timer", str(tmp_path / "absent")) is False
    assert ["systemctl", "daemon-reload"] not in fake.calls


def test_retire_unit_clears_failed_state_even_when_nothing_else_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit left in `failed` is matched by vmalert's SystemdUnitFailed rule, so
    it would page for a unit this module just decided should not exist."""
    fake = _units(
        monkeypatch,
        **{
            "systemctl__is-enabled__--quiet__old.timer": 1,
            "systemctl__is-active__--quiet__old.timer": 3,
        },
    )

    systemd.retire_unit(_ctx(tmp_path), "old.timer", str(tmp_path / "absent"))

    assert ["systemctl", "reset-failed", "old.timer"] in fake.calls


def test_run_once_starts_the_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _units(monkeypatch)

    systemd.run_once(_ctx(tmp_path), "job.service")

    assert ["systemctl", "start", "job.service"] in fake.calls


def test_run_once_raises_when_the_job_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`systemctl start` blocks on a Type=oneshot unit, so a non-zero exit is the
    job failing. The bash called it bare and discarded that."""
    _units(monkeypatch, **{"systemctl__start__job.service": 1})

    with pytest.raises(InstallError, match="job.service failed"):
        systemd.run_once(_ctx(tmp_path), "job.service")


def test_daemon_reload_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _units(monkeypatch)

    systemd.daemon_reload(_ctx(tmp_path))

    assert fake.calls == [["systemctl", "daemon-reload"]]


# ---------------------------------------------------------------------------
# packages.installed — ask, do not ensure
# ---------------------------------------------------------------------------


def test_installed_reports_a_present_package_without_installing_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _apt(monkeypatch, FakeApt({"unattended-upgrades": FakeApt.INSTALLED}))

    assert packages.installed(_ctx(tmp_path), "unattended-upgrades") is True
    assert not any("install" in call for call in fake.calls)


def test_installed_reports_an_absent_package_without_installing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """apt-upgrade must fail rather than install it: pulling unattended-upgrades
    in would change the host's upgrade behaviour as a side effect of a reboot flag."""
    fake = _apt(monkeypatch, FakeApt())

    assert packages.installed(_ctx(tmp_path), "unattended-upgrades") is False
    assert not any("install" in call for call in fake.calls)
