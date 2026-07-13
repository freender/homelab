from __future__ import annotations

import shlex
from pathlib import Path

from .templates import render_template


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def copy_files(source_dir: Path, destination_dir: Path, names: list[str]) -> None:
    for name in names:
        copy_file(source_dir / name, destination_dir / name)


def render_file(template: Path, destination: Path, **context: str) -> None:
    render_template(template, destination, **context)


def write_env_file(destination: Path, values: dict[str, object]) -> None:
    # These files are `source`d by the remote installers as root. Double-quoting is
    # not enough: a value containing $, `, \ or " would be expanded or would break out
    # of the quoting. shlex.quote produces a literal the shell cannot reinterpret.
    lines = [f"{key}={shlex.quote(str(value))}" for key, value in values.items()]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join([*lines, ""]), encoding="utf-8")
