"""Behavior of the `homelab secrets` command group.

These five commands are the only operator-facing entry point into op_secrets,
and they were previously unreached by any test: the suite runs with
HOMELAB_OFFLINE=1, which no longer short-circuits them because each one is a
thin wrapper whose *own* job is exit codes and output shape, not secret
retrieval. op_secrets itself is stubbed here — what `op inject` does belongs to
tests/test_op_secrets.py.

Nothing here touches the real /dev/shm cache or the real 1Password token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from homelab import cli, op_secrets


def _run(*args: str):
    return CliRunner().invoke(cli.main, list(args))


@pytest.fixture(autouse=True)
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the CLI at a tmp repo root so no command can read the real one."""
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    return tmp_path


class TestSecretsDoctor:
    def test_propagates_the_doctor_exit_code_and_repo_root(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[Path, object]] = []
        monkeypatch.setattr(
            cli.op_secrets, "doctor", lambda root, names: seen.append((root, names)) or 0
        )

        result = _run("secrets", "doctor")

        assert result.exit_code == 0
        assert seen == [(repo, None)]  # no names -> None, meaning "the whole catalog"

    def test_failure_exit_code_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli.op_secrets, "doctor", lambda _root, _names: 1)

        assert _run("secrets", "doctor").exit_code == 1

    def test_explicit_names_are_passed_through_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []
        monkeypatch.setattr(
            cli.op_secrets, "doctor", lambda _root, names: seen.append(names) or 0
        )

        assert _run("secrets", "doctor", "alpha", "zulu").exit_code == 0
        assert seen == [("alpha", "zulu")]

    def test_offline_mode_announces_the_reduced_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this line a passing offline run reads as a real `op` check."""
        monkeypatch.setattr(cli, "offline_mode", lambda: True)
        monkeypatch.setattr(cli.op_secrets, "doctor", lambda _root, _names: 0)

        result = _run("secrets", "doctor")

        assert "Offline mode" in result.output

    def test_online_mode_does_not_announce_offline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "offline_mode", lambda: False)
        monkeypatch.setattr(cli.op_secrets, "doctor", lambda _root, _names: 0)

        assert "Offline mode" not in _run("secrets", "doctor").output


class TestSecretsList:
    def test_prints_one_name_per_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli.op_secrets, "list_secret_names", lambda _root: ["alpha", "zulu"]
        )

        result = _run("secrets", "list")

        assert result.exit_code == 0
        assert result.output.splitlines() == ["alpha", "zulu"]

    def test_catalog_error_becomes_a_click_error_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_root):
            raise op_secrets.OpSecretsError("missing secrets catalog: nowhere/catalog.yml")

        monkeypatch.setattr(cli.op_secrets, "list_secret_names", boom)

        result = _run("secrets", "list")

        assert result.exit_code != 0
        assert "missing secrets catalog" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestSecretsRender:
    def test_reports_the_path_and_warns_that_secrets_persist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli.op_secrets, "render_all", lambda _root: tmp_path / "cache")

        result = _run("secrets", "render")

        assert result.exit_code == 0
        assert str(tmp_path / "cache") in result.output
        assert "tmpfs until cache expiry" in result.output

    def test_render_failure_becomes_a_click_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_root):
            raise op_secrets.OpSecretsError("op inject failed for template svc.env.tpl")

        monkeypatch.setattr(cli.op_secrets, "render_all", boom)

        result = _run("secrets", "render")

        assert result.exit_code != 0
        assert "op inject failed" in result.output
        assert "tmpfs until cache expiry" not in result.output  # no false all-clear


class TestSecretsCacheStatus:
    def test_reports_path_ttl_and_each_cached_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cli.op_secrets,
            "cache_info",
            lambda: {
                "path": "/dev/shm/homelab-secret-cache-1000",
                "ttl_seconds": 86400,
                "files": [
                    {"name": "alpha.abc.env", "age_seconds": 120, "size": 64},
                    {"name": "zulu.def.env", "age_seconds": 7, "size": 32},
                ],
            },
        )

        result = _run("secrets", "cache-status")

        assert result.exit_code == 0
        assert "/dev/shm/homelab-secret-cache-1000" in result.output
        assert "86400 seconds" in result.output
        assert "alpha.abc.env age=120s size=64B" in result.output
        assert "zulu.def.env age=7s size=32B" in result.output

    def test_empty_cache_says_none_rather_than_printing_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cli.op_secrets,
            "cache_info",
            lambda: {"path": "/dev/shm/cache", "ttl_seconds": 0, "files": []},
        )

        result = _run("secrets", "cache-status")

        assert result.exit_code == 0
        assert "Files: none" in result.output

    def test_cache_error_becomes_a_click_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom():
            raise op_secrets.OpSecretsError("/dev/shm not available")

        monkeypatch.setattr(cli.op_secrets, "cache_info", boom)

        result = _run("secrets", "cache-status")

        assert result.exit_code != 0
        assert "/dev/shm not available" in result.output


class TestSecretsCacheClear:
    def test_clears_and_confirms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(cli.op_secrets, "clear_cache", lambda: calls.append(True))

        result = _run("secrets", "cache-clear")

        assert result.exit_code == 0
        assert calls == [True]
        assert "secret cache cleared" in result.output

    def test_refusal_becomes_a_click_error_without_the_success_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom():
            raise op_secrets.OpSecretsError("refusing to remove cache not owned by current user")

        monkeypatch.setattr(cli.op_secrets, "clear_cache", boom)

        result = _run("secrets", "cache-clear")

        assert result.exit_code != 0
        assert "refusing to remove" in result.output
        assert "secret cache cleared" not in result.output
