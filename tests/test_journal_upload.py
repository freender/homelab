from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from homelab.modules import journal_upload
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def host_entry(name: str, stacks: list[str]) -> str:
    stack_lines = "".join(f"        - {stack}\n" for stack in stacks)
    return (
        f"{name}:\n"
        "  config:\n"
        "    type: ubuntu\n"
        f"    hostname: {name}.internal\n"
        "    user: root\n"
        "    sshkey: infra\n"
        "  features:\n"
        "    journal-upload:\n"
        "    docker-stacks:\n"
        "      stacks:\n"
        f"{stack_lines}"
    )


def fake_root(tmp_path: Path, *hosts: str) -> Path:
    (tmp_path / "hosts.conf").write_text("".join(hosts), encoding="utf-8")
    module = tmp_path / journal_upload.MODULE_DIR
    shutil.copytree(ROOT / journal_upload.MODULE_DIR / "templates", module / "templates")
    (module / "scripts").mkdir(parents=True)
    (module / "scripts" / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return tmp_path


def test_validate_accepts_the_real_inventory() -> None:
    journal_upload.validate(ROOT)


def test_url_follows_the_victoria_logs_stack(tmp_path: Path) -> None:
    root = fake_root(
        tmp_path, host_entry("logs", ["victoria-logs"]), host_entry("other", ["traefik"])
    )

    assert journal_upload.upload_url(root) == "http://logs.internal:9428/insert/journald"


@pytest.mark.parametrize(
    ("hosts", "found"),
    [
        ((host_entry("a", ["traefik"]),), "none"),
        ((host_entry("a", ["victoria-logs"]), host_entry("b", ["victoria-logs"])), "a, b"),
    ],
)
def test_validate_needs_exactly_one_victoria_logs(
    tmp_path: Path, hosts: tuple[str, ...], found: str
) -> None:
    root = fake_root(tmp_path, *hosts)

    with pytest.raises(ValueError, match=f"exactly one host .* found: {found}$"):
        journal_upload.validate(root)


@pytest.mark.parametrize("missing", ["templates/journal-upload.conf.j2", "scripts/install.py"])
def test_validate_refuses_a_missing_bundle_file(tmp_path: Path, missing: str) -> None:
    root = fake_root(tmp_path, host_entry("logs", ["victoria-logs"]))
    (root / journal_upload.MODULE_DIR / missing).unlink()

    with pytest.raises(ValueError, match="missing"):
        journal_upload.validate(root)


def test_real_host_renders_helm_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendered through deploy_host's dry run: exactly the bytes that would ship."""
    monkeypatch.setattr(journal_upload, "connection_for_host", lambda _root, _host: None)
    monkeypatch.setattr(journal_upload, "diff_many", lambda _conn, _pairs: [])
    root = tmp_path / "repo"
    (root / journal_upload.MODULE_DIR).mkdir(parents=True)
    shutil.copy(ROOT / "hosts.conf", root / "hosts.conf")
    templates = journal_upload.MODULE_DIR + "/templates"
    shutil.copytree(ROOT / templates, root / templates)

    journal_upload.deploy_host(root, "ace", dry_run=True, force=False)

    build = root / journal_upload.MODULE_DIR / "build" / "ace"
    text = (build / "journal-upload.conf").read_text(encoding="utf-8")
    assert "[Upload]\nURL=http://helm.freender.internal:9428/insert/journald\n" in text
    env_text = (build / "env").read_text(encoding="utf-8")
    assert f"JOURNAL_UPLOAD_DEST={journal_upload.REMOTE_CONFIG}" in env_text


# ---------------------------------------------------------------------------
# Remote installer -- apt, systemctl and journalctl faked, state in tmp_path
# ---------------------------------------------------------------------------

INSTALLER_PATH = ROOT / "journal-upload" / "scripts" / "install.py"
HEAD = "s=abc;i=10"


def load_installer():
    spec = importlib.util.spec_from_file_location("journal_upload_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHost:
    """Plays systemctl and journalctl. A (re)start "uploads" by advancing the
    state file's cursor, unless `delivers` is False."""

    def __init__(self, installer, active: bool, enabled: bool, delivers: bool = True) -> None:
        self.installer = installer
        self.active = active
        self.enabled = enabled
        self.delivers = delivers
        self.journal_code = 0
        self.journal_out = json.dumps({"__CURSOR": HEAD, "MESSAGE": "x"})
        self.calls: list[list[str]] = []

    def _upload(self) -> None:
        self.active = True
        if self.delivers:
            state = self.installer.STATE_FILE
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("LAST_CURSOR=s=abc;i=99\n", encoding="utf-8")

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "journalctl":
            return subprocess.CompletedProcess(command, self.journal_code, self.journal_out, "")
        verb = command[1]
        code = 0
        if verb == "is-enabled":
            code = 0 if self.enabled else 1
        elif verb == "is-active":
            code = 0 if self.active else 3
        elif verb == "enable" or verb in ("restart", "start"):
            self.enabled = self.enabled or verb == "enable"
            self._upload()
        return subprocess.CompletedProcess(command, code, "", "")

    def ran(self, verb: str) -> bool:
        return any(call[0] == "systemctl" and call[1] == verb for call in self.calls)


def setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    staged: str = "URL=new\n",
    live: str | None = None,
    state: str | None = None,
    active: bool = False,
    enabled: bool = False,
    delivers: bool = True,
):
    installer = load_installer()
    private = tmp_path / "private"
    monkeypatch.setattr(installer, "PRIVATE_ROOT", private)
    monkeypatch.setattr(installer, "STATE_FILE", private / "systemd" / "journal-upload" / "state")
    fake = FakeHost(installer, active=active, enabled=enabled, delivers=delivers)
    monkeypatch.setattr(installer, "_run", fake)
    monkeypatch.setattr(installer, "_sleep", lambda _seconds: None)
    monkeypatch.setattr("homelab_install.systemd._run", fake)
    monkeypatch.setattr(installer.packages, "ensure", lambda _ctx, *pkgs: fake.calls.append(
        ["apt", *pkgs]))
    if state is not None:
        installer.STATE_FILE.parent.mkdir(parents=True)
        installer.STATE_FILE.write_text(state, encoding="utf-8")

    build = tmp_path / "build" / "box"
    build.mkdir(parents=True)
    (build / "journal-upload.conf").write_text(staged, encoding="utf-8")
    dest = tmp_path / "etc" / "homelab.conf"
    if live is not None:
        dest.parent.mkdir(parents=True)
        dest.write_text(live, encoding="utf-8")
    ctx = InstallContext(
        host="box",
        script_dir=tmp_path,
        build_dir=build,
        env={"JOURNAL_UPLOAD_DEST": str(dest)},
        deploy_env={},
        file_map={},
        force_update=False,
    )
    return installer, ctx, fake, dest


def test_first_install_seeds_the_head_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing state file would upload the whole journal."""
    installer, ctx, fake, dest = setup(tmp_path, monkeypatch)
    seen_at_start: list[str | None] = []
    original = fake._upload

    def upload() -> None:
        seen_at_start.append(installer.read_cursor())
        original()

    fake._upload = upload

    installer.install(ctx)

    assert ["apt", "systemd-journal-remote"] in fake.calls
    assert dest.read_text(encoding="utf-8") == "URL=new\n"
    assert seen_at_start == [HEAD]
    assert ["systemctl", "enable", "--now", installer.UNIT] in fake.calls
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700


def test_seeded_state_matches_what_the_uploader_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, _ctx, _fake, _dest = setup(tmp_path, monkeypatch)

    assert installer.seed_cursor() is True

    text = installer.STATE_FILE.read_text(encoding="utf-8")
    assert text == f"# This is private data. Do not parse.\nLAST_CURSOR={HEAD}\n"


def test_converged_host_is_not_restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer, ctx, fake, _dest = setup(
        tmp_path, monkeypatch, live="URL=new\n", state="LAST_CURSOR=s=old\n",
        active=True, enabled=True,
    )

    installer.install(ctx)

    assert not any(call[0] == "journalctl" for call in fake.calls)
    assert not fake.ran("restart") and not fake.ran("start")
    assert installer.read_cursor() == "s=old"


def test_changed_url_restarts_and_keeps_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing cursor is resumed, never reseeded: reseeding would drop
    whatever was logged while the old URL was failing."""
    installer, ctx, fake, _dest = setup(
        tmp_path, monkeypatch, live="URL=old\n", state="LAST_CURSOR=s=old\n",
        active=True, enabled=True,
    )

    installer.install(ctx)

    assert fake.ran("restart")
    assert not any(call[0] == "journalctl" for call in fake.calls)


def test_lost_state_on_a_running_uploader_restarts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running process keeps its cursor in memory; only a restart reads the seed."""
    installer, ctx, fake, _dest = setup(
        tmp_path, monkeypatch, live="URL=new\n", active=True, enabled=True
    )

    installer.install(ctx)

    assert fake.ran("restart")


def test_dead_uploader_is_started_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, ctx, fake, _dest = setup(
        tmp_path, monkeypatch, live="URL=new\n", state="LAST_CURSOR=s=old\n",
        active=False, enabled=True,
    )

    installer.install(ctx)

    assert fake.ran("start")
    assert installer.read_cursor() == "s=abc;i=99"


def test_no_delivery_fails_the_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Active is not delivered: a wrong URL leaves the unit restarting forever."""
    installer, ctx, _fake, _dest = setup(tmp_path, monkeypatch, delivers=False)
    clock = iter([0.0, 10.0, 20.0, 31.0])
    monkeypatch.setattr(installer, "_now", lambda: next(clock))

    with pytest.raises(InstallError, match="delivered nothing within 30s"):
        installer.install(ctx)


def test_inactive_after_start_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer, ctx, fake, _dest = setup(tmp_path, monkeypatch)
    original = fake._upload

    def crash() -> None:
        original()
        fake.active = False

    fake._upload = crash

    with pytest.raises(InstallError, match="not active after deploy"):
        installer.install(ctx)


@pytest.mark.parametrize(("code", "out"), [(1, ""), (0, ""), (0, "{}"), (0, "[]")])
def test_unreadable_journal_head_is_refused_before_any_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, out: str
) -> None:
    installer, ctx, fake, _dest = setup(tmp_path, monkeypatch)
    fake.journal_code = code
    fake.journal_out = out

    with pytest.raises(InstallError):
        installer.install(ctx)

    assert not installer.STATE_FILE.exists()
    assert not fake.ran("enable")


def test_missing_staged_config_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer, ctx, fake, _dest = setup(tmp_path, monkeypatch)
    (ctx.build_dir / "journal-upload.conf").unlink()

    with pytest.raises(InstallError, match="missing staged journal-upload config"):
        installer.install(ctx)

    assert fake.calls == []


def test_state_without_cursor_reads_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, _ctx, _fake, _dest = setup(tmp_path, monkeypatch, state="# nothing\n")

    assert installer.read_cursor() is None
