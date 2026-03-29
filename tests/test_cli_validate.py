from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from homelab import cli


def test_validate_reports_offline_mode(monkeypatch, tmp_path: Path) -> None:
    messages: list[str] = []

    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_run_command", lambda command, cwd: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "ordered_modules", lambda: ["alpha", "beta"])
    monkeypatch.setattr(cli, "execute_module", lambda *args: 0)
    monkeypatch.setattr(cli, "offline_mode", lambda: True)
    monkeypatch.setattr(cli, "print_sub", messages.append)

    (tmp_path / "hosts.conf").write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["validate"])

    assert result.exit_code == 0
    assert "Offline mode enabled; remote SSH diffs are skipped" in messages


def test_validate_fails_when_any_module_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_run_command", lambda command, cwd: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "ordered_modules", lambda: ["alpha", "beta"])
    monkeypatch.setattr(cli, "offline_mode", lambda: False)

    def fake_execute_module(module_name: str, host: str, dry_run: bool, force: bool) -> int:
        return 1 if module_name == "beta" else 0

    monkeypatch.setattr(cli, "execute_module", fake_execute_module)

    (tmp_path / "hosts.conf").write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["validate"])

    assert result.exit_code != 0
    assert "dry-run failures: beta" in result.output
