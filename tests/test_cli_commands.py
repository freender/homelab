"""Behavior of the two CLI commands that carry real branching: `hosts stacks` and `deploy`.

Both were previously reached only incidentally — `hosts stacks` not at all, and
`deploy` only through the unknown-host regression in test_safety_regressions.py — so
their error paths and the `all`-module aggregation were unasserted despite being the
entry point every operator and every `/ship` run goes through.

`deploy` is exercised with `execute_module` stubbed: the dispatch, the failure
aggregation, and the exit codes are this file's subject, while what a module actually
does belongs to that module's own tests and tests/test_dry_run_all_modules.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from homelab import cli

HOSTS_CONF = """
helm:
  config:
    type: lxc
    hostname: helm.internal
    user: freender
    sshkey: infra
  features:
    docker-stacks:
      stacks:
        - grafana
        - vmagent
tower:
  config:
    type: lxc
    hostname: tower.internal
    user: freender
    sshkey: infra
  features:
    docker-stacks:
      stacks:
        - plex
        - vmagent
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features: {}
"""


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A tmp repo root whose hosts.conf the CLI will read."""
    (tmp_path / "hosts.conf").write_text(HOSTS_CONF.lstrip(), encoding="utf-8")
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    return tmp_path


def _run(*args: str):
    return CliRunner().invoke(cli.main, list(args))


class TestListStacks:
    def test_lists_every_stack_with_its_host_sorted(self, repo: Path) -> None:
        result = _run("hosts", "stacks")

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            "grafana\thelm",
            "plex\ttower",
            "vmagent\thelm",
            "vmagent\ttower",
        ]

    def test_host_filter_restricts_to_that_host(self, repo: Path) -> None:
        result = _run("hosts", "stacks", "--host", "tower")

        assert result.exit_code == 0
        assert result.output.splitlines() == ["plex\ttower", "vmagent\ttower"]

    def test_stack_filter_answers_which_hosts_run_an_app(self, repo: Path) -> None:
        result = _run("hosts", "stacks", "--stack", "vmagent")

        assert result.exit_code == 0
        assert result.output.splitlines() == ["vmagent\thelm", "vmagent\ttower"]

    def test_both_filters_intersect(self, repo: Path) -> None:
        result = _run("hosts", "stacks", "--host", "helm", "--stack", "vmagent")

        assert result.exit_code == 0
        assert result.output.splitlines() == ["vmagent\thelm"]

    def test_host_without_docker_stacks_is_an_error_not_empty_output(self, repo: Path) -> None:
        """Silence would read as 'that host runs nothing', which is a different fact."""
        result = _run("hosts", "stacks", "--host", "ace")

        assert result.exit_code != 0
        assert "host 'ace' does not enable docker-stacks" in result.output

    def test_unknown_host_is_rejected(self, repo: Path) -> None:
        result = _run("hosts", "stacks", "--host", "orbit")

        assert result.exit_code != 0
        assert "host 'orbit' does not enable docker-stacks" in result.output

    def test_undeclared_stack_is_an_error_not_empty_output(self, repo: Path) -> None:
        result = _run("hosts", "stacks", "--stack", "nosuchapp")

        assert result.exit_code != 0
        assert "no host declares stack 'nosuchapp'" in result.output


class TestDeploy:
    def test_unknown_host_is_rejected_before_module_dispatch(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(cli, "execute_module", lambda *args: calls.append(args) or 0)

        result = _run("deploy", "docker", "orbit")

        assert result.exit_code != 0
        assert "unknown host 'orbit'" in result.output
        assert calls == []

    def test_unknown_module_is_rejected(self, repo: Path) -> None:
        result = _run("deploy", "nosuchmodule", "helm")

        assert result.exit_code != 0
        assert "Unknown or unported module: nosuchmodule" in result.output

    def test_host_all_skips_the_inventory_check(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`all` is not a host in hosts.conf; each module resolves it itself."""
        calls: list[tuple] = []
        monkeypatch.setattr(cli, "execute_module", lambda *args: calls.append(args) or 0)
        monkeypatch.setitem(cli.MODULES, "docker", object())

        result = _run("deploy", "docker", "all")

        assert result.exit_code == 0
        assert calls == [("docker", "all", False, False)]

    def test_single_module_passes_flags_through_and_returns_its_exit_code(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(cli, "execute_module", lambda *args: calls.append(args) or 3)
        monkeypatch.setitem(cli.MODULES, "docker", object())

        result = _run("deploy", "--dry-run", "--force", "docker", "helm")

        assert result.exit_code == 3
        assert calls == [("docker", "helm", True, True)]

    def test_module_all_runs_every_ordered_module_in_order(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli, "ordered_modules", lambda: ["first", "second", "third"])
        calls: list[tuple] = []
        monkeypatch.setattr(cli, "execute_module", lambda *args: calls.append(args) or 0)

        result = _run("deploy", "all", "helm")

        assert result.exit_code == 0
        assert [call[0] for call in calls] == ["first", "second", "third"]
        assert "Deploy complete!" in result.output

    def test_module_all_names_every_failed_module_and_does_not_claim_success(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A partial deploy must fail loudly; the failing names are the whole point."""
        monkeypatch.setattr(cli, "ordered_modules", lambda: ["ok", "bad", "worse"])
        monkeypatch.setattr(
            cli,
            "execute_module",
            lambda module, *rest: 0 if module == "ok" else 1,
        )

        result = _run("deploy", "all", "helm")

        assert result.exit_code != 0
        assert "Failed modules: bad worse" in result.output
        assert "Deploy complete!" not in result.output

    def test_module_all_continues_past_a_failure(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One broken module must not strand the remaining ones undeployed."""
        monkeypatch.setattr(cli, "ordered_modules", lambda: ["bad", "later"])
        calls: list[str] = []

        def execute(module: str, *rest: object) -> int:
            calls.append(module)
            return 1 if module == "bad" else 0

        monkeypatch.setattr(cli, "execute_module", execute)

        result = _run("deploy", "all", "helm")

        assert result.exit_code != 0
        assert calls == ["bad", "later"]

    def test_confirm_upgrade_sets_the_env_gate(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[str | None] = []
        monkeypatch.delenv(cli.CONFIRM_UPGRADE_ENV, raising=False)
        monkeypatch.setattr(
            cli,
            "execute_module",
            lambda *args: seen.append(cli.os.environ.get(cli.CONFIRM_UPGRADE_ENV)) or 0,
        )
        monkeypatch.setitem(cli.MODULES, "pve-upgrade", object())

        result = _run("deploy", "--confirm-upgrade", "pve-upgrade", "helm")

        assert result.exit_code == 0
        assert seen == ["1"]

    def test_env_gate_stays_unset_without_the_flag(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The gate is what stops an unattended `deploy` from dist-upgrading a node."""
        seen: list[str | None] = []
        monkeypatch.delenv(cli.CONFIRM_UPGRADE_ENV, raising=False)
        monkeypatch.setattr(
            cli,
            "execute_module",
            lambda *args: seen.append(cli.os.environ.get(cli.CONFIRM_UPGRADE_ENV)) or 0,
        )
        monkeypatch.setitem(cli.MODULES, "pve-upgrade", object())

        result = _run("deploy", "pve-upgrade", "helm")

        assert result.exit_code == 0
        assert seen == [None]
