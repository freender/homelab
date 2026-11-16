"""`./validate`'s ShellCheck step lints what the repo publishes, not the whole tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from homelab import cli


def _write(path: Path, content: str = "#!/bin/bash\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_only_tracked_or_unignored_scripts_are_linted(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("mutants/\n.venv/\nbuild/\n", encoding="utf-8")
    for rel in ("mod/scripts/install.sh", "mutants/mod/scripts/install.sh",
                ".venv/lib/activate.sh", "mod/build/host/x.sh", "mod/scripts/notes.txt",
                "mod/files/bin/tool", "mutants/mod/files/bin/tool"):
        _write(tmp_path / rel)

    assert cli.shell_lint_targets(tmp_path) == [str(tmp_path / "mod/files/bin/tool"),
                                                str(tmp_path / "mod/scripts/install.sh")]


def test_without_git_the_walk_skips_copies_and_vendored_trees(tmp_path: Path) -> None:
    for rel in ("mod/scripts/install.sh", "mutants/mod/scripts/install.sh",
                ".venv/lib/activate.sh", ".bin/tool.sh", "build/x.sh",
                "mod/files/bin/tool", ".venv/bin/activate"):
        _write(tmp_path / rel)

    assert cli.shell_lint_targets(tmp_path) == [str(tmp_path / "mod/files/bin/tool"),
                                                str(tmp_path / "mod/scripts/install.sh")]


@pytest.mark.parametrize(("name", "content", "expected"), [
    ("x.sh", "", True),
    ("tool", "#!/bin/bash\n", True),
    ("tool", "#!/bin/sh -e\n", True),
    ("tool", "#!/usr/bin/env bash\n", True),
    ("tool", "#! /usr/bin/env sh\n", True),
    ("tool", "#!/usr/bin/env python3\n", False),
    ("tool", "#!/bin/zsh\n", False),
    ("tool", "#!\n", False),
    ("tool", "#!/usr/bin/env\n", False),
    ("tool", "echo no shebang\n", False),
    ("tool", "", False),
    ("x.tpl", "#!/bin/bash\n", False),
])
def test_shell_scripts_are_named_sh_or_carry_a_shell_shebang(
        tmp_path: Path, name: str, content: str, expected: bool) -> None:
    _write(tmp_path / name, content)

    assert cli.is_shell_script(tmp_path / name) is expected


def test_directories_are_not_shell_scripts(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "d.sh").mkdir()

    assert not cli.is_shell_script(tmp_path / "bin")
    assert not cli.is_shell_script(tmp_path / "d.sh")
