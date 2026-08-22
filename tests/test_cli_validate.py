from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from homelab import cli

ROOT = Path(__file__).resolve().parents[1]


def _write_node_down_pair(root: Path, scraped: list[str], covered: str, extra: str = "") -> None:
    """Build the minimal scrape.yml / node-down.yml pair the coverage check reads."""
    scrape = root / "monitoring-config" / "configs"
    scrape.mkdir(parents=True, exist_ok=True)
    targets = "\n".join(
        f'      - targets: ["{host}:9100"]\n        labels:\n          host: {host}'
        for host in scraped
    )
    (scrape / "scrape.yml").write_text(
        f"scrape_configs:\n  - job_name: pve-node\n    static_configs:\n{targets}\n",
        encoding="utf-8",
    )

    rules = root / "vmalert-rules" / "configs"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "node-down.yml").write_text(
        "groups:\n"
        "  - name: node-down\n"
        "    rules:\n"
        f"{extra}"
        "      - alert: NodeDown\n"
        f'        expr: up{{job="pve-node", host=~"{covered}"}} == 0\n',
        encoding="utf-8",
    )


def test_validate_runs_ruff_and_pytest_when_available(monkeypatch, tmp_path: Path) -> None:
    # Per-module dry-run now lives in tests/test_dry_run_all_modules.py (parametrized,
    # coverage-instrumented) rather than a bespoke for-loop in `validate`, so this test
    # only asserts the subprocess steps run, not their content.
    commands: list[list[str]] = []

    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_run_command", lambda command, cwd: commands.append(command))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    (tmp_path / "hosts.conf").write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["validate"])

    assert result.exit_code == 0
    assert any("ruff" in command for command in commands)
    assert any("pytest" in command for command in commands)


def test_validate_warns_when_pytest_missing(monkeypatch, tmp_path: Path) -> None:
    messages: list[str] = []

    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_run_command", lambda command, cwd: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_module_available", lambda name: name != "pytest")
    monkeypatch.setattr(cli, "print_warn", messages.append)

    (tmp_path / "hosts.conf").write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["validate"])

    assert result.exit_code == 0
    assert any("skipping tests and per-module dry-run" in message for message in messages)


def test_node_down_coverage_accepts_the_live_repo_config() -> None:
    """The real scrape.yml/node-down.yml pair must stay in sync, not just synthetic ones."""
    cli.check_node_down_coverage(ROOT)


def test_node_down_coverage_flags_a_scraped_host_with_no_alert(tmp_path: Path) -> None:
    _write_node_down_pair(tmp_path, scraped=["ace", "newbox"], covered="ace")

    with pytest.raises(click.ClickException, match="no NodeDown coverage: newbox"):
        cli.check_node_down_coverage(tmp_path)


def test_node_down_coverage_honours_a_declared_exclusion(tmp_path: Path) -> None:
    _write_node_down_pair(
        tmp_path,
        scraped=["ace", "ghost"],
        covered="ace",
        extra="      # nodedown-exclude: ghost\n",
    )

    cli.check_node_down_coverage(tmp_path)


def test_node_down_coverage_flags_a_rule_for_an_unscraped_host(tmp_path: Path) -> None:
    _write_node_down_pair(tmp_path, scraped=["ace"], covered="ace|retired")

    with pytest.raises(click.ClickException, match="does not scrape: retired"):
        cli.check_node_down_coverage(tmp_path)
