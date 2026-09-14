from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# The shared remote-installer library. On a target it is imported off PYTHONPATH,
# set by `stage_and_run_remote_installer`; here it is imported in-process, which
# freender/homelab-ops#31 decision 4 permits in this direction only (`src/homelab/`
# and `tests/` may import `homelab_install`, never the reverse).
PY_LIB = ROOT / "lib" / "py"

for path in (SRC, PY_LIB):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def isolate_homelab_offline() -> Iterator[None]:
    """Restore HOMELAB_OFFLINE around every test.

    `homelab validate` deliberately forces offline mode with a bare
    `os.environ.setdefault("HOMELAB_OFFLINE", "1")` (src/homelab/cli.py), which is
    correct for the CLI but escapes into the pytest process the moment
    `test_cli_validate.py` invokes it through CliRunner. Nothing undoes it.

    That leak created a real ordering dependency: `test_safety_regressions.py`'s
    keepalived test needs offline mode but never sets it, and passed only because
    `test_cli_validate` sorts earlier and had already turned it on process-wide.
    Run that file on its own and it failed:

        $ pytest tests/test_safety_regressions.py
        ValueError: 1Password CLI `op` not found in PATH.

    CI was green purely on alphabetical collection order. Snapshotting the variable
    per test makes tests that need offline mode declare it, and makes tests that
    need it *off* immune to a neighbour turning it on.
    """
    sentinel = object()
    previous: object = os.environ.get("HOMELAB_OFFLINE", sentinel)
    try:
        yield
    finally:
        if previous is sentinel:
            os.environ.pop("HOMELAB_OFFLINE", None)
        else:
            os.environ["HOMELAB_OFFLINE"] = str(previous)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force offline secret resolution for a test.

    Modules fall back to `secrets/templates/<name>.env.tpl.example` instead of
    shelling out to the `op` CLI, so a test can render real artifacts with no
    1Password session and no network.
    """
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")
