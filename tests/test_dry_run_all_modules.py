"""Offline dry-run every registered module against the real hosts.conf.

This used to be a bespoke for-loop inside `homelab validate` (see git history on
`cli.py`). Moving it into pytest gets two things a hand-rolled loop can't: it runs
under `--cov`, so coverage data exists for every module (previously `zfs_automation.py`,
21% of all Python here, had two dedicated tests and no other signal), and a single
module can be re-run in isolation with `pytest -k <module_name>` instead of always
dry-running the whole fleet.

`homelab validate` still gates on this: it runs the full pytest suite, which includes
this file.
"""

from __future__ import annotations

import pytest

from homelab.cli import execute_module
from homelab.modules import ordered_modules


@pytest.fixture(autouse=True)
def _offline_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dry-run must never hit SSH or the `op` CLI. Modules fall back to the `.example`
    # secret templates under secrets/templates/ when this is set (see op_secrets.py).
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")


def test_there_are_modules_to_dry_run() -> None:
    # Guard the guard: an empty registry would make the parametrize below iterate
    # nothing and pass vacuously.
    assert ordered_modules()


@pytest.mark.parametrize("module_name", ordered_modules())
def test_module_dry_runs_cleanly(module_name: str) -> None:
    exit_code = execute_module(module_name, "all", True, False)
    assert exit_code == 0, f"{module_name} failed its offline dry-run against hosts.conf"
