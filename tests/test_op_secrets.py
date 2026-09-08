"""Unit tests for the op_secrets flagged cluster: secret_file, _render_with_op,
_cache_dir, clear_cache, cleanup, and doctor. These are the credential-handling
code paths that HOMELAB_OFFLINE=1 short-circuits everywhere else in the suite,
so nothing else in the test tree exercises them.

TMPFS_BASE is monkeypatched to tmp_path throughout: nothing here ever touches
the real /dev/shm.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from homelab import op_secrets


@pytest.fixture(autouse=True)
def reset_op_secrets_globals() -> Iterator[None]:
    """Snapshot/restore module-level session state around every test.

    op_secrets keeps process-global state (_session_dir, _rendered,
    _session_initialized) that production code relies on persisting across
    calls within one deploy invocation. Left alone across tests it would leak:
    a test that sets _session_initialized=True would make a later test skip
    ensure_op_session's real checks.
    """
    session_dir = op_secrets._session_dir
    rendered = dict(op_secrets._rendered)
    initialized = op_secrets._session_initialized
    op_secrets._session_dir = None
    op_secrets._rendered.clear()
    op_secrets._session_initialized = False
    try:
        yield
    finally:
        op_secrets._session_dir = session_dir
        op_secrets._rendered.clear()
        op_secrets._rendered.update(rendered)
        op_secrets._session_initialized = initialized


def _write_catalog(
    root: Path,
    name: str,
    template_content: str = "VALUE={{ op://Homelab/x/password }}\n",
    example_content: str | None = "VALUE=example\n",
) -> op_secrets.SecretEntry:
    """Write (or add to) secrets/catalog.yml, then return the new entry.

    Additive: calling this more than once with different names accumulates
    entries in the same catalog.yml instead of clobbering earlier ones.
    """
    templates_dir = root / "secrets" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    template = templates_dir / f"{name}.env.tpl"
    template.write_text(template_content, encoding="utf-8")
    if example_content is not None:
        example = templates_dir / f"{name}.env.tpl.example"
        example.write_text(example_content, encoding="utf-8")

    catalog_file = root / "secrets" / "catalog.yml"
    lines = catalog_file.read_text(encoding="utf-8").splitlines() if catalog_file.is_file() else [
        "secrets:"
    ]
    lines.append(f"  {name}:")
    lines.append(f"    template: secrets/templates/{name}.env.tpl")
    catalog_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return op_secrets.load_catalog(root)[name]


# ---------------------------------------------------------------------------
# _cache_dir
# ---------------------------------------------------------------------------


def test_cache_dir_raises_when_tmpfs_base_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path / "does-not-exist")

    with pytest.raises(op_secrets.OpSecretsError, match="not available"):
        op_secrets._cache_dir()


def test_cache_dir_raises_when_path_is_a_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    blocker = tmp_path / f"{op_secrets.CACHE_PREFIX}-{__import__('os').getuid()}"
    blocker.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(op_secrets.OpSecretsError, match="not a directory"):
        op_secrets._cache_dir()


def test_cache_dir_raises_when_owned_by_another_uid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    # Fake uid: the directory _cache_dir() creates is real-owned, but the code
    # compares that ownership against os.getuid(), which we've redirected.
    monkeypatch.setattr(op_secrets.os, "getuid", lambda: 999999)

    with pytest.raises(op_secrets.OpSecretsError, match="must be owned by current user"):
        op_secrets._cache_dir()


def test_cache_dir_tightens_overly_permissive_existing_directory(
    monkeypatch, tmp_path: Path
) -> None:
    import os

    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    path = tmp_path / f"{op_secrets.CACHE_PREFIX}-{os.getuid()}"
    path.mkdir(mode=0o700)
    path.chmod(0o755)  # world-readable; _cache_dir must tighten this back down

    result = op_secrets._cache_dir()

    assert result == path
    assert (path.stat().st_mode & 0o777) == 0o700


def test_cache_dir_creates_directory_on_first_call(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)

    result = op_secrets._cache_dir()

    assert result.is_dir()
    assert (result.stat().st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


def test_clear_cache_is_noop_when_directory_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)

    op_secrets.clear_cache()  # must not raise


def test_clear_cache_refuses_non_directory(monkeypatch, tmp_path: Path) -> None:
    import os

    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    blocker = tmp_path / f"{op_secrets.CACHE_PREFIX}-{os.getuid()}"
    blocker.write_text("nope\n", encoding="utf-8")

    with pytest.raises(op_secrets.OpSecretsError, match="not a directory"):
        op_secrets.clear_cache()


def test_clear_cache_refuses_directory_owned_by_another_uid(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    # Patch getuid *before* creating the directory so clear_cache() looks up
    # the same (fake) path name it will later stat — whose real on-disk owner
    # is still this test process, producing the mismatch under test.
    monkeypatch.setattr(op_secrets.os, "getuid", lambda: 999999)
    path = tmp_path / f"{op_secrets.CACHE_PREFIX}-999999"
    path.mkdir(mode=0o700)

    with pytest.raises(op_secrets.OpSecretsError, match="refusing to remove"):
        op_secrets.clear_cache()


def test_clear_cache_shreds_and_removes_files(monkeypatch, tmp_path: Path) -> None:
    import os

    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: None)  # unlink fallback
    path = tmp_path / f"{op_secrets.CACHE_PREFIX}-{os.getuid()}"
    path.mkdir(mode=0o700)
    (path / "a.env").write_text("secret-a\n", encoding="utf-8")
    (path / "b.env").write_text("secret-b\n", encoding="utf-8")
    op_secrets._rendered["a"] = path / "a.env"

    op_secrets.clear_cache()

    assert not path.exists()
    assert op_secrets._rendered == {}


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_is_noop_when_no_session_dir() -> None:
    op_secrets._session_dir = None

    op_secrets.cleanup()  # must not raise, nothing to do


def test_cleanup_removes_session_directory_via_unlink_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: None)
    session = tmp_path / "session"
    session.mkdir()
    (session / "a.env").write_text("secret\n", encoding="utf-8")
    op_secrets._session_dir = session
    op_secrets._rendered["a"] = session / "a.env"

    op_secrets.cleanup()

    assert not session.exists()
    assert op_secrets._session_dir is None
    assert op_secrets._rendered == {}


def test_cleanup_shreds_files_when_shred_available(monkeypatch, tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    target = session / "a.env"
    target.write_text("secret\n", encoding="utf-8")
    op_secrets._session_dir = session

    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: "/usr/bin/shred")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        calls.append(cmd)
        Path(cmd[-1]).unlink(missing_ok=True)  # simulate shred -u actually removing it
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(op_secrets.subprocess, "run", fake_run)

    op_secrets.cleanup()

    assert calls == [["/usr/bin/shred", "-u", "-n", "1", str(target)]]
    assert not session.exists()


def test_cleanup_swallows_oserror_from_shred_and_still_removes_dir(
    monkeypatch, tmp_path: Path
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "a.env").write_text("secret\n", encoding="utf-8")
    op_secrets._session_dir = session

    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: "/usr/bin/shred")

    def raising_run(*_args, **_kwargs):
        raise OSError("shred unavailable at runtime")

    monkeypatch.setattr(op_secrets.subprocess, "run", raising_run)

    op_secrets.cleanup()  # must not raise despite the shred failure

    assert not session.exists()  # rmtree still cleans up what shred could not


# ---------------------------------------------------------------------------
# _render_with_op
# ---------------------------------------------------------------------------


def test_render_with_op_success_writes_and_chmods_destination(
    monkeypatch, tmp_path: Path
) -> None:
    template = tmp_path / "t.env.tpl"
    template.write_text("VALUE={{ op://x }}\n", encoding="utf-8")
    destination = tmp_path / "out" / "rendered.env"

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(cmd)
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        Path(cmd[-1]).write_text("VALUE=rendered\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(op_secrets.subprocess, "run", fake_run)

    op_secrets._render_with_op(template, destination)

    assert calls == [
        ["op", "inject", "--force", "--in-file", str(template), "--out-file", str(destination)]
    ]
    assert destination.read_text(encoding="utf-8") == "VALUE=rendered\n"
    assert (destination.stat().st_mode & 0o777) == 0o600


def test_render_with_op_overwrites_preexisting_destination(
    monkeypatch, tmp_path: Path
) -> None:
    template = tmp_path / "t.env.tpl"
    template.write_text("VALUE={{ op://x }}\n", encoding="utf-8")
    destination = tmp_path / "rendered.env"
    destination.write_text("stale-content-from-a-previous-render\n", encoding="utf-8")

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        # Simulate op writing nothing extra; destination should already be
        # truncated to empty by the pre-create step regardless.
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(op_secrets.subprocess, "run", fake_run)

    op_secrets._render_with_op(template, destination)

    assert destination.read_text(encoding="utf-8") == ""


def test_render_with_op_failure_removes_destination_and_never_leaks_template_body(
    monkeypatch, tmp_path: Path
) -> None:
    template = tmp_path / "t.env.tpl"
    secret_looking_content = "VALUE={{ op://Homelab/super-secret-item/password }}\n"
    template.write_text(secret_looking_content, encoding="utf-8")
    destination = tmp_path / "rendered.env"

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="line1\nline2\nline3\nline4 (the real error)\n"
        )

    monkeypatch.setattr(op_secrets.subprocess, "run", fake_run)

    with pytest.raises(op_secrets.OpSecretsError) as excinfo:
        op_secrets._render_with_op(template, destination)

    message = str(excinfo.value)
    assert "line2 | line3 | line4 (the real error)" in message  # last 3 lines only
    assert "line1" not in message
    assert "super-secret-item" not in message  # template body/vault path never surfaced
    assert not destination.exists()


def test_render_with_op_failure_with_no_output_uses_placeholder(
    monkeypatch, tmp_path: Path
) -> None:
    template = tmp_path / "t.env.tpl"
    template.write_text("VALUE=x\n", encoding="utf-8")
    destination = tmp_path / "rendered.env"

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(op_secrets.subprocess, "run", fake_run)

    with pytest.raises(op_secrets.OpSecretsError, match="no output"):
        op_secrets._render_with_op(template, destination)


# ---------------------------------------------------------------------------
# secret_file
# ---------------------------------------------------------------------------


def test_secret_file_offline_returns_example(monkeypatch, tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    result = op_secrets.secret_file(tmp_path, "svc")

    assert result == tmp_path / "secrets" / "templates" / "svc.env.tpl.example"


def test_secret_file_offline_without_example_raises(monkeypatch, tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc", example_content=None)
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    with pytest.raises(op_secrets.OpSecretsError, match="no example file"):
        op_secrets.secret_file(tmp_path, "svc")


def test_secret_file_unknown_name_raises(monkeypatch, tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    with pytest.raises(op_secrets.OpSecretsError, match="unknown secret"):
        op_secrets.secret_file(tmp_path, "does-not-exist")


def test_secret_file_returns_already_rendered_path_without_reloading_catalog(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    sentinel = tmp_path / "already-rendered.env"
    op_secrets._rendered["svc"] = sentinel

    def boom(_root):
        raise AssertionError("catalog should not be reloaded for an already-rendered secret")

    monkeypatch.setattr(op_secrets, "load_catalog", boom)

    assert op_secrets.secret_file(tmp_path, "svc") == sentinel


def test_secret_file_uses_fresh_cache_without_invoking_op(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    entry = _write_catalog(tmp_path, "svc")

    cache_path = op_secrets._cache_path(entry)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("VALUE=cached\n", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise AssertionError("op should not be invoked when the cache is fresh")

    monkeypatch.setattr(op_secrets, "_render_with_op", boom)

    result = op_secrets.secret_file(tmp_path, "svc")

    assert result == cache_path
    assert op_secrets._rendered["svc"] == cache_path


def test_secret_file_renders_and_caches_on_stale_miss(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    entry = _write_catalog(tmp_path, "svc")

    render_calls: list[tuple[Path, Path]] = []

    def fake_render(template: Path, destination: Path) -> None:
        render_calls.append((template, destination))
        destination.write_text("VALUE=fresh\n", encoding="utf-8")

    monkeypatch.setattr(op_secrets, "_render_with_op", fake_render)

    result = op_secrets.secret_file(tmp_path, "svc")

    assert result == op_secrets._cache_path(entry)
    assert result.read_text(encoding="utf-8") == "VALUE=fresh\n"
    assert (result.stat().st_mode & 0o777) == 0o600
    assert op_secrets._rendered["svc"] == result
    # Rendered against a *.tmp path, then atomically replaced onto the real
    # cache path — never rendered directly onto the final name.
    assert render_calls[0][1] != result
    assert not render_calls[0][1].exists()


def test_secret_file_cleans_up_temp_file_when_render_fails(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    _write_catalog(tmp_path, "svc")

    captured: dict[str, Path] = {}

    def failing_render(_template: Path, destination: Path) -> None:
        captured["temp_path"] = destination
        destination.write_text("partial\n", encoding="utf-8")
        raise op_secrets.OpSecretsError("op inject failed")

    monkeypatch.setattr(op_secrets, "_render_with_op", failing_render)

    with pytest.raises(op_secrets.OpSecretsError):
        op_secrets.secret_file(tmp_path, "svc")

    assert not captured["temp_path"].exists()
    assert "svc" not in op_secrets._rendered


def test_secret_file_prunes_stale_cache_entries_before_rendering(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: None)
    entry = _write_catalog(tmp_path, "svc")

    cache_dir = op_secrets._cache_dir()
    stale = cache_dir / "svc.deadbeefdeadbeefdeadbeef.env"
    stale.write_text("old\n", encoding="utf-8")

    def fake_render(_template: Path, destination: Path) -> None:
        destination.write_text("new\n", encoding="utf-8")

    monkeypatch.setattr(op_secrets, "_render_with_op", fake_render)

    op_secrets.secret_file(tmp_path, "svc")

    assert not stale.exists()
    assert op_secrets._cache_path(entry).exists()


def test_secret_file_disabled_cache_renders_into_session_dir(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setenv("HOMELAB_SECRET_CACHE_TTL", "0")
    _write_catalog(tmp_path, "svc")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    monkeypatch.setattr(op_secrets, "_ensure_session_dir", lambda: session_dir)

    def fake_render(_template: Path, destination: Path) -> None:
        destination.write_text("VALUE=session\n", encoding="utf-8")

    monkeypatch.setattr(op_secrets, "_render_with_op", fake_render)

    result = op_secrets.secret_file(tmp_path, "svc")

    assert result == session_dir / "svc.env"
    assert op_secrets._rendered["svc"] == result


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_offline_reports_success_for_every_entry(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    assert op_secrets.doctor(tmp_path) == 0
    assert "[offline] svc: example OK" in capsys.readouterr().out


def test_doctor_offline_fails_when_example_missing(monkeypatch, tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc", example_content=None)
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    assert op_secrets.doctor(tmp_path) == 1


def test_doctor_offline_fails_for_explicit_unknown_name(
    monkeypatch, tmp_path: Path
) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    assert op_secrets.doctor(tmp_path, names=["does-not-exist"]) == 1


def test_doctor_online_returns_1_when_authentication_fails(
    monkeypatch, tmp_path: Path
) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)

    def boom() -> None:
        raise op_secrets.OpSecretsError("no token")

    monkeypatch.setattr(op_secrets, "ensure_op_session", boom)

    assert op_secrets.doctor(tmp_path) == 1


def test_doctor_online_success_renders_each_entry_and_always_cleans_up(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    monkeypatch.setattr(op_secrets, "_ensure_session_dir", lambda: session_dir)

    cleanup_calls: list[bool] = []
    monkeypatch.setattr(op_secrets, "cleanup", lambda: cleanup_calls.append(True))

    def fake_render(_template: Path, destination: Path) -> None:
        destination.write_text("VALUE=x\n", encoding="utf-8")

    monkeypatch.setattr(op_secrets, "_render_with_op", fake_render)

    assert op_secrets.doctor(tmp_path) == 0
    assert "OK    svc" in capsys.readouterr().out
    assert cleanup_calls == [True]


def test_doctor_online_partial_failure_still_cleans_up_and_returns_1(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _write_catalog(tmp_path, "good")
    _write_catalog(tmp_path, "bad")
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    monkeypatch.setattr(op_secrets, "_ensure_session_dir", lambda: session_dir)

    cleanup_calls: list[bool] = []
    monkeypatch.setattr(op_secrets, "cleanup", lambda: cleanup_calls.append(True))

    def fake_render(template: Path, destination: Path) -> None:
        if "bad" in template.name:
            raise op_secrets.OpSecretsError("op inject failed for bad")
        destination.write_text("VALUE=x\n", encoding="utf-8")

    monkeypatch.setattr(op_secrets, "_render_with_op", fake_render)

    assert op_secrets.doctor(tmp_path) == 1
    out = capsys.readouterr().out
    assert "OK    good" in out
    assert "FAIL  bad" in out
    assert cleanup_calls == [True]  # finally still runs after a mid-loop failure


def test_doctor_online_unknown_explicit_name_is_a_failure(
    monkeypatch, tmp_path: Path
) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    monkeypatch.setattr(op_secrets, "_ensure_session_dir", lambda: session_dir)
    monkeypatch.setattr(op_secrets, "cleanup", lambda: None)

    assert op_secrets.doctor(tmp_path, names=["does-not-exist"]) == 1
