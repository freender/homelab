"""Host identity reads shared by installers."""

from __future__ import annotations

import shlex
from pathlib import Path

OS_RELEASE = "/etc/os-release"


def os_release(path: str | Path = OS_RELEASE) -> dict[str, str]:
    """`/etc/os-release` as a dict, parsed rather than sourced; empty when absent.

    Values are unquoted with shell rules and a trailing `# comment` dropped.
    """
    values: dict[str, str] = {}
    release = Path(path)
    if not release.is_file():
        return values
    for line in release.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip():
            tokens = shlex.split(raw, comments=True)
            values[key.strip()] = tokens[0] if tokens else ""
    return values
