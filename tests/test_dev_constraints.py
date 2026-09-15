"""The interpreter running the suite must match constraints.txt.

CI and the repo .venv once resolved different dependency sets from the same
pyproject ranges — pytest-cov 6.3.0 against 7.1.0, whose subprocess-coverage hooks
differ — and a CI failure could not be reproduced locally. Pinning both to one file
only holds if something notices when an environment stops honouring it.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Must be installed wherever the suite runs. Everything else in the file (the
# `mutation` extra's closure) is checked only when present, because CI installs
# `.[dev]` alone.
REQUIRED = {
    "click", "fabric", "jinja2", "pyyaml", "rich",
    "pytest", "pytest-cov", "coverage", "ruff",
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "constraints.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, pinned = line.partition("==")
        assert sep, f"constraints.txt entry is not an exact pin: {line!r}"
        pins[_normalize(name)] = pinned
    return pins


def test_every_required_package_is_pinned() -> None:
    assert REQUIRED <= _pins().keys()


def test_installed_versions_match_constraints() -> None:
    mismatched = []
    for name, pinned in _pins().items():
        try:
            installed = version(name)
        except PackageNotFoundError:
            if name in REQUIRED:
                mismatched.append(f"{name}: not installed, pinned {pinned}")
            continue
        if installed != pinned:
            mismatched.append(f"{name}: installed {installed}, pinned {pinned}")
    assert not mismatched, (
        "environment has drifted from constraints.txt; run "
        "`python -m pip install -c constraints.txt '.[dev,mutation]'`:\n  "
        + "\n  ".join(mismatched)
    )
