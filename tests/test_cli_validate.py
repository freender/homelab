from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from homelab import cli, crap

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
    assert any("skipping tests, per-module dry-run, and CRAP" in message for message in messages)


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


def _crap_row(name: str, score: float) -> crap.CrapRow:
    return crap.CrapRow(
        score=score, complexity=5, coverage=0.0, filename="src/homelab/mod.py", name=name, line=1
    )


def _stub_rows(monkeypatch, rows: list[crap.CrapRow] | None) -> None:
    """Bypass the coverage-report build; the gate's logic is what is under test."""
    monkeypatch.setattr(cli, "crap_rows", lambda root: rows)


def test_crap_gate_fails_on_a_function_the_baseline_does_not_own(monkeypatch, tmp_path) -> None:
    _stub_rows(monkeypatch, [_crap_row("fresh", 12.0)])

    with pytest.raises(click.ClickException, match="CRAP gate failed at 10"):
        cli.check_crap(tmp_path)


def test_crap_gate_passes_a_grandfathered_function(monkeypatch, tmp_path: Path) -> None:
    crap.write_baseline(tmp_path / crap.BASELINE_FILENAME, [_crap_row("old", 31.1)])
    _stub_rows(monkeypatch, [_crap_row("old", 31.1)])

    cli.check_crap(tmp_path)


def test_crap_gate_fails_when_a_grandfathered_function_gets_worse(monkeypatch, tmp_path) -> None:
    crap.write_baseline(tmp_path / crap.BASELINE_FILENAME, [_crap_row("old", 31.1)])
    _stub_rows(monkeypatch, [_crap_row("old", 44.0)])

    with pytest.raises(click.ClickException, match="WORSE"):
        cli.check_crap(tmp_path)


def test_crap_gate_warns_but_passes_when_an_entry_can_be_dropped(monkeypatch, tmp_path) -> None:
    messages: list[str] = []
    monkeypatch.setattr(cli, "print_warn", messages.append)
    crap.write_baseline(tmp_path / crap.BASELINE_FILENAME, [_crap_row("old", 31.1)])
    _stub_rows(monkeypatch, [_crap_row("old", 3.0)])

    cli.check_crap(tmp_path)

    assert any("--update-baseline" in message for message in messages)


def test_crap_gate_skips_rather_than_fails_when_coverage_is_unusable(monkeypatch, tmp_path) -> None:
    # A broken metrics pipeline must not be indistinguishable from crappy code.
    _stub_rows(monkeypatch, None)

    cli.check_crap(tmp_path)


def test_crap_report_update_baseline_writes_only_the_over_gate_rows(monkeypatch, tmp_path) -> None:
    _stub_rows(monkeypatch, [_crap_row("bad", 12.0), _crap_row("fine", 2.0)])

    assert cli.run_crap_report(tmp_path, update_baseline=True, top=10, threshold=10.0) == 0
    written = crap.load_baseline(tmp_path / crap.BASELINE_FILENAME)
    assert written == {"src/homelab/mod.py::bad": 12.0}


def test_crap_report_exits_nonzero_without_coverage(monkeypatch, tmp_path: Path) -> None:
    _stub_rows(monkeypatch, None)

    assert cli.run_crap_report(tmp_path, update_baseline=False, top=10, threshold=10.0) == 1


def test_crap_report_truncates_to_top_n(monkeypatch, tmp_path: Path) -> None:
    lines: list[str] = []
    monkeypatch.setattr(cli, "print_sub", lines.append)
    _stub_rows(monkeypatch, [_crap_row(f"f{index}", 12.0) for index in range(5)])

    cli.run_crap_report(tmp_path, update_baseline=False, top=2, threshold=10.0)

    assert lines[-1] == "... and 3 more over 10"


def test_repo_baseline_matches_the_gate_threshold() -> None:
    """A hand-edited baseline written against a looser gate would silently exempt code."""
    path = ROOT / crap.BASELINE_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["threshold"] == crap.FAIL_THRESHOLD
