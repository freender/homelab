"""`./validate`'s ShellCheck step lints what the repo publishes, not the whole tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from homelab import cli


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n", encoding="utf-8")


def test_only_tracked_or_unignored_scripts_are_linted(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("mutants/\n.venv/\nbuild/\n", encoding="utf-8")
    for rel in ("mod/scripts/install.sh", "mutants/mod/scripts/install.sh",
                ".venv/lib/activate.sh", "mod/build/host/x.sh", "mod/scripts/notes.txt"):
        _write(tmp_path / rel)

    assert cli.shell_lint_targets(tmp_path) == [str(tmp_path / "mod/scripts/install.sh")]


def test_without_git_the_walk_skips_copies_and_vendored_trees(tmp_path: Path) -> None:
    for rel in ("mod/scripts/install.sh", "mutants/mod/scripts/install.sh",
                ".venv/lib/activate.sh", ".bin/tool.sh", "build/x.sh"):
        _write(tmp_path / rel)

    assert cli.shell_lint_targets(tmp_path) == [str(tmp_path / "mod/scripts/install.sh")]
