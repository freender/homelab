"""Offline dry-run every registered module against the real hosts.conf.

This used to be a bespoke for-loop inside `homelab validate` (see git history on
`cli.py`). Moving it into pytest gets two things a hand-rolled loop can't: it runs
under `--cov`, so coverage data exists for every module (previously `zfs_automation.py`,
21% of all Python here, had two dedicated tests and no other signal), and a single
module can be re-run in isolation with `pytest -k <module_name>` instead of always
dry-running the whole fleet.

`homelab validate` still gates on this: it runs the full pytest suite, which includes
this file.

**This file asserts what the dry-run did, not just that it exited 0.** The 2026-09-12
mutation sweep showed why: inverting the applicability guard in
`module_support.run_module_deploy` (`if not hosts:` -> `if hosts:`) makes every module
skip every applicable host, and the old `assert exit_code == 0` stayed green because the
skip path also returns 0. One surviving mutant there silently disabled 20 modules at
once. `test_module_dry_run_visits_every_enabled_host` is the assertion that kills it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from homelab.cli import execute_module, repo_root
from homelab.deploy import DeploySession
from homelab.hosts import default_registry
from homelab.modules import all_registered_modules

# pve-autoinstall is the one module whose dry-run deliberately returns before
# `session.run`: it reports the rendered PDM answer plan (`_report_dry_run`) instead of
# walking hosts, and its live run targets the single PDM host rather than the feature's
# own host list. Exempt from the visited-hosts assertion, not from the dry-run itself —
# `test_exempt_modules_really_do_skip_the_session` pins the exemption so it cannot
# quietly grow to cover a module that has genuinely stopped deploying.
SKIPS_SESSION_IN_DRY_RUN = frozenset({"pve-autoinstall"})


@pytest.fixture(autouse=True)
def _offline_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dry-run must never hit SSH or the `op` CLI. Modules fall back to the `.example`
    # secret templates under secrets/templates/ when this is set (see op_secrets.py).
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")


@pytest.fixture
def visited_hosts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record the hosts each module hands to `DeploySession.run`, then run them anyway.

    Patched on the class, not on an instance, because `execute_module` constructs the
    session itself. The original method still runs, so this observes the real dry-run
    rather than replacing it.
    """
    recorded: list[str] = []
    original = DeploySession.run

    def recording_run(
        self: DeploySession, deploy_host: Callable[[str], None], hosts: list[str]
    ) -> None:
        recorded.extend(hosts)
        original(self, deploy_host, hosts)

    monkeypatch.setattr(DeploySession, "run", recording_run)
    yield recorded


def enabled_hosts(module_name: str) -> list[str]:
    """Hosts hosts.conf enables `module_name` on.

    Feature names and module-registry keys are the same string in both directions —
    `cli.check_feature_registry` fails validate if they ever diverge — so this is the
    independent expectation for which hosts a module must visit.
    """
    return default_registry(repo_root()).list_hosts(feature=module_name)


def test_there_are_modules_to_dry_run() -> None:
    # Guard the guard: an empty registry would make the parametrize below iterate
    # nothing and pass vacuously.
    assert all_registered_modules()


# Deliberately all_registered_modules(), not ordered_modules(): a module excluded
# from `deploy all` (include_in_all=False) still needs its dry-run to stay honest.
@pytest.mark.parametrize("module_name", all_registered_modules())
def test_module_dry_runs_cleanly(module_name: str) -> None:
    exit_code = execute_module(module_name, "all", True, False)
    assert exit_code == 0, f"{module_name} failed its offline dry-run against hosts.conf"


@pytest.mark.parametrize(
    "module_name",
    [name for name in all_registered_modules() if name not in SKIPS_SESSION_IN_DRY_RUN],
)
def test_module_dry_run_visits_every_enabled_host(
    module_name: str, visited_hosts: list[str]
) -> None:
    expected = enabled_hosts(module_name)
    # Non-vacuity: a module no host enables would make the equality below trivially
    # true. `check_feature_registry` only warns about that case, so assert it here.
    assert expected, f"{module_name} is enabled on no host; the assertion below is vacuous"

    assert execute_module(module_name, "all", True, False) == 0
    assert visited_hosts == expected, (
        f"{module_name} dry-ran {visited_hosts or 'no hosts'} but hosts.conf enables it "
        f"on {expected}"
    )


@pytest.mark.parametrize("module_name", sorted(SKIPS_SESSION_IN_DRY_RUN))
def test_exempt_modules_really_do_skip_the_session(
    module_name: str, visited_hosts: list[str]
) -> None:
    """Pin the exemption from both sides.

    The module must still be enabled somewhere (otherwise it belongs in the orphan
    warning, not in an exemption list) and must still be the deliberate no-session case
    the list claims it is. If either stops holding, delete the entry rather than
    widening it.
    """
    assert enabled_hosts(module_name)
    assert execute_module(module_name, "all", True, False) == 0
    assert visited_hosts == []


def test_single_host_target_visits_only_that_host(visited_hosts: list[str]) -> None:
    """Targeting one host narrows the deploy to it, and does not fall back to `all`."""
    hosts = enabled_hosts("base-packages")
    assert len(hosts) > 1, "need a multi-host module for this to distinguish anything"

    assert execute_module("base-packages", hosts[-1], True, False) == 0
    assert visited_hosts == [hosts[-1]]


def test_host_without_the_feature_deploys_nowhere(visited_hosts: list[str]) -> None:
    """A real host that does not enable the feature is skipped, not deployed to."""
    registry = default_registry(repo_root())
    enabled = set(enabled_hosts("wsl-conf"))
    other = next(host for host in registry.list_hosts() if host not in enabled)

    assert execute_module("wsl-conf", other, True, False) == 0
    assert visited_hosts == []


def test_unknown_host_fails_and_deploys_nowhere(visited_hosts: list[str]) -> None:
    """An unknown target is an error, never a silent no-op that reports success."""
    assert execute_module("base-packages", "no-such-host", True, False) == 1
    assert visited_hosts == []


def test_repo_root_is_the_checkout_containing_hosts_conf() -> None:
    # execute_module() resolves the root itself, so every assertion above is only about
    # this repo if repo_root() really points at it.
    assert (repo_root() / "hosts.conf").is_file()
    assert repo_root() == Path(__file__).resolve().parents[1]
