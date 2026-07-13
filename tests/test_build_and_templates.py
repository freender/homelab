from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from jinja2 import UndefinedError

from homelab.build import copy_file, copy_files, render_file, write_env_file
from homelab.deploy import force_env, prepare_build_dir
from homelab.templates import render_template


def test_copy_file_and_copy_files_create_parent_dirs(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "one.txt").write_text("one\n", encoding="utf-8")
    (source_dir / "two.txt").write_text("two\n", encoding="utf-8")

    destination_dir = tmp_path / "dest" / "nested"

    copy_file(source_dir / "one.txt", destination_dir / "one.txt")
    copy_files(source_dir, destination_dir, ["two.txt"])

    assert (destination_dir / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert (destination_dir / "two.txt").read_text(encoding="utf-8") == "two\n"


def test_render_template_and_render_file_write_output(tmp_path: Path) -> None:
    template = tmp_path / "templates" / "config.tpl"
    template.parent.mkdir()
    template.write_text("host={{ HOST }}\n", encoding="utf-8")

    rendered_direct = tmp_path / "out" / "direct.conf"
    rendered_wrapper = tmp_path / "out" / "wrapper.conf"

    render_template(template, rendered_direct, HOST="ace")
    render_file(template, rendered_wrapper, HOST="bray")

    assert rendered_direct.read_text(encoding="utf-8") == "host=ace\n"
    assert rendered_wrapper.read_text(encoding="utf-8") == "host=bray\n"


def test_render_template_raises_for_missing_context(tmp_path: Path) -> None:
    template = tmp_path / "missing.tpl"
    template.write_text("host={{ HOST }}\n", encoding="utf-8")

    with pytest.raises(UndefinedError):
        render_template(template, tmp_path / "out.conf")


def test_write_env_file_writes_shell_safe_values(tmp_path: Path) -> None:
    destination = tmp_path / "build" / "env"

    write_env_file(destination, {"NAME": "helm", "ENABLED": True, "COUNT": 3})

    # shlex.quote leaves simple tokens bare; they still `source` correctly.
    assert destination.read_text(encoding="utf-8") == "NAME=helm\nENABLED=True\nCOUNT=3\n"


def test_write_env_file_neutralizes_shell_metacharacters(tmp_path: Path) -> None:
    destination = tmp_path / "build" / "env"

    # These files are sourced by the remote installers as root. A value carrying a
    # command substitution must survive as a literal, not execute.
    write_env_file(destination, {"EVIL": 'a$(id)b"c', "SCHEDULE": "*-*-* 08:00:00"})

    sourced = subprocess.run(
        ["bash", "-c", f'. "{destination}"; printf "%s\\n%s" "$EVIL" "$SCHEDULE"'],
        capture_output=True,
        text=True,
        check=True,
    )

    assert sourced.stdout == 'a$(id)b"c\n*-*-* 08:00:00'


def test_prepare_build_dir_rotates_previous_build(tmp_path: Path) -> None:
    build_dir = tmp_path / "build" / "ace"
    build_dir.mkdir(parents=True)
    (build_dir / "old.txt").write_text("old\n", encoding="utf-8")

    prepare_build_dir(build_dir)

    previous_dir = build_dir.with_name("ace.prev")
    assert build_dir.is_dir()
    assert not (build_dir / "old.txt").exists()
    assert (previous_dir / "old.txt").read_text(encoding="utf-8") == "old\n"


def test_force_env_returns_expected_flag_values() -> None:
    assert force_env(True) == {"FORCE_UPDATE": "true"}
    assert force_env(False) == {"FORCE_UPDATE": "false"}
