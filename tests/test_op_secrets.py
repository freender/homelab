"""Unit tests for the op_secrets flagged cluster: secret_file, _render_with_op,
_cache_dir, clear_cache, cleanup, and doctor. These are the credential-handling
code paths that HOMELAB_OFFLINE=1 short-circuits everywhere else in the suite,
so nothing else in the test tree exercises them.

TMPFS_BASE is monkeypatched to tmp_path throughout: nothing here ever touches
the real /dev/shm.
"""

from __future__ import annotations

import os
import signal
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

    OP_SERVICE_ACCOUNT_TOKEN is restored here rather than via monkeypatch
    because ensure_op_session writes it into os.environ itself; monkeypatch
    only rolls back assignments it made, so an unguarded run would leak a
    token-shaped value into the rest of the session.
    """
    session_dir = op_secrets._session_dir
    rendered = dict(op_secrets._rendered)
    initialized = op_secrets._session_initialized
    token_env = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")
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
        os.environ.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        if token_env is not None:
            os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = token_env


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


# ---------------------------------------------------------------------------
# load_catalog and its per-entry helpers
# ---------------------------------------------------------------------------


def _write_raw_catalog(root: Path, body: str) -> None:
    catalog_file = root / "secrets" / "catalog.yml"
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    catalog_file.write_text(body, encoding="utf-8")


def test_load_catalog_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(op_secrets.OpSecretsError, match="missing secrets catalog"):
        op_secrets.load_catalog(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "",  # empty file -> safe_load returns None
        "- not-a-mapping\n",  # top level is a list
        "secrets:\n",  # key present but null
        "secrets: {}\n",  # key present but empty
    ],
)
def test_load_catalog_without_usable_secrets_key_raises(tmp_path: Path, body: str) -> None:
    _write_raw_catalog(tmp_path, body)

    with pytest.raises(op_secrets.OpSecretsError, match="no `secrets:` entries defined"):
        op_secrets.load_catalog(tmp_path)


def test_load_catalog_entry_must_be_a_mapping(tmp_path: Path) -> None:
    _write_raw_catalog(tmp_path, "secrets:\n  svc: just-a-string\n")

    with pytest.raises(op_secrets.OpSecretsError, match="entry 'svc' must be a mapping"):
        op_secrets.load_catalog(tmp_path)


@pytest.mark.parametrize("template_value", ["", "   ", "null", "[]"])
def test_load_catalog_entry_requires_a_template_path(
    tmp_path: Path, template_value: str
) -> None:
    _write_raw_catalog(tmp_path, f"secrets:\n  svc:\n    template: {template_value}\n")

    with pytest.raises(op_secrets.OpSecretsError, match="entry 'svc' missing template path"):
        op_secrets.load_catalog(tmp_path)


def test_load_catalog_entry_template_must_exist(tmp_path: Path) -> None:
    _write_raw_catalog(
        tmp_path, "secrets:\n  svc:\n    template: secrets/templates/gone.env.tpl\n"
    )

    with pytest.raises(op_secrets.OpSecretsError, match="entry 'svc' template not found"):
        op_secrets.load_catalog(tmp_path)


def test_load_catalog_carries_description_and_conventional_example(tmp_path: Path) -> None:
    entry = _write_catalog(tmp_path, "svc")
    _write_raw_catalog(
        tmp_path,
        "secrets:\n"
        "  svc:\n"
        "    template: secrets/templates/svc.env.tpl\n"
        "    description: '  PBS backup credentials  '\n",
    )

    result = op_secrets.load_catalog(tmp_path)["svc"]

    assert result.template == entry.template
    assert result.example == tmp_path / "secrets" / "templates" / "svc.env.tpl.example"
    assert result.description == "PBS backup credentials"  # whitespace stripped
    assert result.filename == "svc.env"


def test_load_catalog_explicit_example_overrides_the_convention(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc")  # also writes svc.env.tpl.example
    explicit = tmp_path / "secrets" / "templates" / "custom.example"
    explicit.write_text("VALUE=custom\n", encoding="utf-8")
    _write_raw_catalog(
        tmp_path,
        "secrets:\n"
        "  svc:\n"
        "    template: secrets/templates/svc.env.tpl\n"
        "    example: secrets/templates/custom.example\n",
    )

    assert op_secrets.load_catalog(tmp_path)["svc"].example == explicit


def test_load_catalog_explicit_example_that_is_missing_yields_none(tmp_path: Path) -> None:
    # A named-but-absent example must not fall back to the <template>.example
    # convention, or an online-only secret would silently pass offline checks.
    _write_catalog(tmp_path, "svc")
    _write_raw_catalog(
        tmp_path,
        "secrets:\n"
        "  svc:\n"
        "    template: secrets/templates/svc.env.tpl\n"
        "    example: secrets/templates/nope.example\n",
    )

    assert op_secrets.load_catalog(tmp_path)["svc"].example is None


def test_load_catalog_without_any_example_yields_none(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc", example_content=None)

    assert op_secrets.load_catalog(tmp_path)["svc"].example is None


def test_list_secret_names_is_sorted(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "zulu")
    _write_catalog(tmp_path, "alpha")

    assert op_secrets.list_secret_names(tmp_path) == ["alpha", "zulu"]


# ---------------------------------------------------------------------------
# _find_token_path / _ensure_secure_token_path / ensure_op_session
#
# The credential-loading path proper. Nothing else in the suite reaches it:
# every other test either runs with HOMELAB_OFFLINE=1 or stubs
# ensure_op_session out entirely.
# ---------------------------------------------------------------------------


def test_find_token_path_returns_the_first_candidate_that_exists(
    monkeypatch, tmp_path: Path
) -> None:
    preferred = tmp_path / "homelab.token"
    fallback = tmp_path / "service-account-token"
    preferred.write_text("ops_preferred\n", encoding="utf-8")
    fallback.write_text("ops_fallback\n", encoding="utf-8")
    monkeypatch.setattr(op_secrets, "TOKEN_PATHS", (preferred, fallback))

    assert op_secrets._find_token_path() == preferred


def test_find_token_path_falls_back_to_the_legacy_name(monkeypatch, tmp_path: Path) -> None:
    preferred = tmp_path / "homelab.token"
    fallback = tmp_path / "service-account-token"
    fallback.write_text("ops_fallback\n", encoding="utf-8")
    monkeypatch.setattr(op_secrets, "TOKEN_PATHS", (preferred, fallback))

    assert op_secrets._find_token_path() == fallback


def test_find_token_path_names_every_candidate_when_none_exist(
    monkeypatch, tmp_path: Path
) -> None:
    candidates = (tmp_path / "homelab.token", tmp_path / "service-account-token")
    monkeypatch.setattr(op_secrets, "TOKEN_PATHS", candidates)

    with pytest.raises(op_secrets.OpSecretsError) as excinfo:
        op_secrets._find_token_path()

    message = str(excinfo.value)
    for candidate in candidates:
        assert str(candidate) in message


def test_ensure_secure_token_path_returns_stripped_token(tmp_path: Path) -> None:
    token_file = tmp_path / "homelab.token"
    token_file.write_text("  ops_abc123\n\n", encoding="utf-8")
    token_file.chmod(0o600)

    assert op_secrets._ensure_secure_token_path(token_file) == "ops_abc123"


def test_ensure_secure_token_path_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(op_secrets.OpSecretsError, match="token not found"):
        op_secrets._ensure_secure_token_path(tmp_path / "absent.token")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666, 0o700 | 0o044])
def test_ensure_secure_token_path_rejects_group_or_world_access(
    tmp_path: Path, mode: int
) -> None:
    token_file = tmp_path / "homelab.token"
    token_file.write_text("ops_abc123\n", encoding="utf-8")
    token_file.chmod(mode)

    with pytest.raises(op_secrets.OpSecretsError, match="permissions are too open"):
        op_secrets._ensure_secure_token_path(token_file)


def test_ensure_secure_token_path_rejects_file_owned_by_another_uid(
    monkeypatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "homelab.token"
    token_file.write_text("ops_abc123\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setattr(op_secrets.os, "getuid", lambda: 999999)

    with pytest.raises(op_secrets.OpSecretsError, match="must be owned by the current user"):
        op_secrets._ensure_secure_token_path(token_file)


def test_ensure_secure_token_path_rejects_empty_file(tmp_path: Path) -> None:
    token_file = tmp_path / "homelab.token"
    token_file.write_text("   \n", encoding="utf-8")
    token_file.chmod(0o600)

    with pytest.raises(op_secrets.OpSecretsError, match="is empty"):
        op_secrets._ensure_secure_token_path(token_file)


def test_ensure_op_session_exports_token_and_then_short_circuits(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    token_file = tmp_path / "homelab.token"
    token_file.write_text("ops_abc123\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setattr(op_secrets, "TOKEN_PATHS", (token_file,))
    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: "/usr/bin/op")

    op_secrets.ensure_op_session()

    assert os.environ["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_abc123"
    assert op_secrets._session_initialized is True

    # Second call must not re-read the file: proving the guard, not the read.
    token_file.unlink()
    op_secrets.ensure_op_session()


def test_ensure_op_session_keeps_a_preexisting_token_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_from_the_environment")
    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: "/usr/bin/op")

    def boom() -> Path:
        raise AssertionError("token file must not be read when the env var is already set")

    monkeypatch.setattr(op_secrets, "_find_token_path", boom)

    op_secrets.ensure_op_session()

    assert os.environ["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_from_the_environment"


def test_ensure_op_session_offline_skips_op_entirely(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    def boom(_name: str) -> str:
        raise AssertionError("offline mode must not look for the op binary")

    monkeypatch.setattr(op_secrets.shutil, "which", boom)

    op_secrets.ensure_op_session()

    assert op_secrets._session_initialized is True


def test_ensure_op_session_requires_the_op_binary(monkeypatch) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setattr(op_secrets.shutil, "which", lambda _name: None)

    with pytest.raises(op_secrets.OpSecretsError, match="`op` not found in PATH"):
        op_secrets.ensure_op_session()

    assert op_secrets._session_initialized is False


# ---------------------------------------------------------------------------
# _ensure_session_dir / _install_signal_handlers
# ---------------------------------------------------------------------------


def test_ensure_session_dir_creates_a_private_tmpfs_dir_and_arms_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    registered: list[object] = []
    monkeypatch.setattr(op_secrets.atexit, "register", registered.append)
    handlers_installed: list[bool] = []
    monkeypatch.setattr(
        op_secrets, "_install_signal_handlers", lambda: handlers_installed.append(True)
    )

    session = op_secrets._ensure_session_dir()

    assert session.is_dir()
    assert session.parent == tmp_path
    assert session.name.startswith(op_secrets.TMPFS_PREFIX)
    assert (session.stat().st_mode & 0o777) == 0o700
    assert registered == [op_secrets.cleanup]
    assert handlers_installed == [True]


def test_ensure_session_dir_is_idempotent_within_a_process(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets.atexit, "register", lambda _fn: None)
    monkeypatch.setattr(op_secrets, "_install_signal_handlers", lambda: None)

    first = op_secrets._ensure_session_dir()
    second = op_secrets._ensure_session_dir()

    assert first == second
    assert len(list(tmp_path.iterdir())) == 1  # no second mkdtemp


def test_ensure_session_dir_refuses_when_tmpfs_is_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path / "no-dev-shm")

    with pytest.raises(op_secrets.OpSecretsError, match="cannot render secrets to tmpfs"):
        op_secrets._ensure_session_dir()


def test_install_signal_handlers_covers_int_term_and_hup(monkeypatch) -> None:
    installed: dict[int, object] = {}
    monkeypatch.setattr(
        op_secrets.signal, "signal", lambda sig, handler: installed.setdefault(sig, handler)
    )

    op_secrets._install_signal_handlers()

    assert set(installed) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def test_install_signal_handlers_tolerates_a_non_main_thread(monkeypatch) -> None:
    def refuse(_sig, _handler):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(op_secrets.signal, "signal", refuse)

    op_secrets._install_signal_handlers()  # must not propagate


def test_signal_handler_shreds_then_re_raises_the_default_disposition(monkeypatch) -> None:
    installed: dict[int, object] = {}

    def record(sig, handler):
        installed[sig] = handler

    monkeypatch.setattr(op_secrets.signal, "signal", record)
    op_secrets._install_signal_handlers()
    handler = installed[signal.SIGTERM]

    cleanup_calls: list[bool] = []
    monkeypatch.setattr(op_secrets, "cleanup", lambda: cleanup_calls.append(True))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(op_secrets.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    handler(signal.SIGTERM, None)

    assert cleanup_calls == [True]  # secrets shredded before the process dies
    assert installed[signal.SIGTERM] is signal.SIG_DFL  # disposition restored
    assert kills == [(os.getpid(), signal.SIGTERM)]


# ---------------------------------------------------------------------------
# cache_info
# ---------------------------------------------------------------------------


def test_cache_info_reports_an_absent_cache_as_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.delenv("HOMELAB_SECRET_CACHE_TTL", raising=False)

    info = op_secrets.cache_info()

    assert info["path"] == str(tmp_path / f"{op_secrets.CACHE_PREFIX}-{os.getuid()}")
    assert info["ttl_seconds"] == op_secrets.DEFAULT_CACHE_TTL_SECONDS
    assert info["files"] == []


def test_cache_info_lists_env_files_with_age_and_size(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setenv("HOMELAB_SECRET_CACHE_TTL", "60")
    cache_dir = tmp_path / f"{op_secrets.CACHE_PREFIX}-{os.getuid()}"
    cache_dir.mkdir(mode=0o700)
    (cache_dir / "svc.abc.env").write_text("VALUE=x\n", encoding="utf-8")
    (cache_dir / "not-a-secret.txt").write_text("ignored\n", encoding="utf-8")
    os.utime(cache_dir / "svc.abc.env", (0, __import__("time").time() - 120))

    info = op_secrets.cache_info()

    assert info["ttl_seconds"] == 60
    assert [entry["name"] for entry in info["files"]] == ["svc.abc.env"]  # *.env only
    assert info["files"][0]["size"] == len("VALUE=x\n")
    assert info["files"][0]["age_seconds"] >= 119


def test_cache_ttl_seconds_rejects_a_non_integer(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_SECRET_CACHE_TTL", "twelve")

    with pytest.raises(op_secrets.OpSecretsError, match="must be an integer"):
        op_secrets.cache_ttl_seconds()


def test_cache_ttl_seconds_floors_a_negative_value_at_zero(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_SECRET_CACHE_TTL", "-5")

    assert op_secrets.cache_ttl_seconds() == 0


# ---------------------------------------------------------------------------
# render_all
# ---------------------------------------------------------------------------


def test_render_all_offline_points_at_the_templates_dir(monkeypatch, tmp_path: Path) -> None:
    _write_catalog(tmp_path, "svc")
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    assert op_secrets.render_all(tmp_path) == tmp_path / op_secrets.TEMPLATES_DIR


def test_render_all_renders_every_entry_into_the_shared_cache(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    _write_catalog(tmp_path, "alpha")
    _write_catalog(tmp_path, "zulu")

    rendered: list[str] = []

    def fake_render(template: Path, destination: Path) -> None:
        rendered.append(template.name)
        destination.write_text("VALUE=x\n", encoding="utf-8")

    monkeypatch.setattr(op_secrets, "_render_with_op", fake_render)

    result = op_secrets.render_all(tmp_path)

    assert result == op_secrets._cache_dir()
    assert sorted(rendered) == ["alpha.env.tpl", "zulu.env.tpl"]
    assert sorted(op_secrets._rendered) == ["alpha", "zulu"]


def test_render_all_with_cache_disabled_returns_the_session_dir(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    monkeypatch.setenv("HOMELAB_SECRET_CACHE_TTL", "0")
    monkeypatch.setattr(op_secrets, "TMPFS_BASE", tmp_path)
    monkeypatch.setattr(op_secrets, "ensure_op_session", lambda: None)
    monkeypatch.setattr(op_secrets.atexit, "register", lambda _fn: None)
    monkeypatch.setattr(op_secrets, "_install_signal_handlers", lambda: None)
    _write_catalog(tmp_path, "svc")

    monkeypatch.setattr(
        op_secrets,
        "_render_with_op",
        lambda _template, destination: destination.write_text("VALUE=x\n", encoding="utf-8"),
    )

    result = op_secrets.render_all(tmp_path)

    assert result == op_secrets._session_dir
    assert (result / "svc.env").read_text(encoding="utf-8") == "VALUE=x\n"
