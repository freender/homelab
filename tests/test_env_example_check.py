"""Tests for the .env.example placeholder check.

This repo is public, so these tests deliberately use throwaway fixture values.
The real private domain must never appear here either -- see AGENTS.md "Public
Repo Boundary".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from homelab.cli import check_env_example_placeholders

ROOT = Path(__file__).resolve().parents[1]


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_real_repo_env_examples_are_clean() -> None:
    """Every `.env.example` currently in the repo must pass -- regression guard."""
    check_env_example_placeholders(ROOT)


def test_ignores_non_env_example_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"stack/.env": 'TOKEN="a-real-looking-secret-value"\n'})
    check_env_example_placeholders(repo)


@pytest.mark.parametrize(
    "line",
    [
        'TOKEN="<PLACEHOLDER_TOKEN>"',
        "TOKEN=",
        'TOKEN=""',
        "PORT=9162",
        "PUID=1000",
        'FLAG="true"',
        "LOG_LEVEL=info",
        'MIN_FILE_AGE="5m"',
        'PATH_VAR="/mnt/cache/appdata/x"',
        'SOCKET="unix:///var/run/docker.sock"',
        'DOMAIN="example.net"',
        'URL="https://app.example.net"',
        "FINGERPRINT=xx:xx:xx:xx:xx:xx",
        "SECRET=replace-with-real-value",
        "SECRET=CHANGEME",
        'JINJA="{{ SOME_VAR }}"',
    ],
)
def test_allows_placeholder_shaped_values(tmp_path: Path, line: str) -> None:
    repo = _git_repo(tmp_path, {"stack/.env.example": line + "\n"})
    check_env_example_placeholders(repo)


@pytest.mark.parametrize(
    "line",
    [
        # Not "AA..." after the colon -- a real-shaped Telegram token would (correctly)
        # also trip check_public_repo_leaks's own secret-shape scan of this file.
        'TELEGRAM_TOKEN="1234567890:zzFakeRealLookingTokenValueHere1234"',
        'DOMAIN="my-actual-homelab-domain.net"',
        'API_KEY="sk-thisIsNotAPlaceholder1234567890"',
        "PASSWORD=hunter2",
    ],
)
def test_flags_real_looking_values(tmp_path: Path, line: str) -> None:
    repo = _git_repo(tmp_path, {"stack/.env.example": line + "\n"})
    with pytest.raises(click.ClickException) as excinfo:
        check_env_example_placeholders(repo)
    assert "is not a placeholder value" in str(excinfo.value)


def test_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    repo = _git_repo(
        tmp_path,
        {"stack/.env.example": "# a real-looking-secret in a comment=yes\n\nTOKEN=<X>\n"},
    )
    check_env_example_placeholders(repo)


def test_finding_names_the_key_and_its_line(tmp_path: Path) -> None:
    """The finding must point at the offending assignment, not just the file.

    Both halves were unasserted: the key could be replaced by the value, and the
    line number could be off by one, with every other test still passing.
    """
    body = "# header\nPORT=9162\nAPI_KEY=sk-thisIsNotAPlaceholder1234567890\n"
    repo = _git_repo(tmp_path, {"stack/.env.example": body})

    with pytest.raises(click.ClickException) as excinfo:
        check_env_example_placeholders(repo)

    assert "stack/.env.example:3: API_KEY is not a placeholder value" in str(excinfo.value)


def test_a_skipped_line_does_not_abandon_the_file(tmp_path: Path) -> None:
    """Comments and non-assignment lines are skipped individually, not terminally.

    The offending assignment is last, after one of each kind of skipped line, so
    any `continue` that became a `break` would lose it.
    """
    body = "\n".join(
        [
            "# a comment",
            "",
            "not an assignment at all",
            "PORT=9162",
            "PASSWORD=hunter2",
        ]
    )
    repo = _git_repo(tmp_path, {"stack/.env.example": body})

    with pytest.raises(click.ClickException) as excinfo:
        check_env_example_placeholders(repo)

    assert "PASSWORD is not a placeholder value" in str(excinfo.value)


def test_an_unreadable_env_example_does_not_abandon_the_scan(tmp_path: Path) -> None:
    """Sorts first in `git ls-files`, so a terminal skip would hide the later leak."""
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "z").mkdir()
    (repo / "a" / ".env.example").write_bytes(b"KEY=\xff\xfe\x00\x80\xc3\x28\n")
    (repo / "z" / ".env.example").write_text("PASSWORD=hunter2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    with pytest.raises(click.ClickException) as excinfo:
        check_env_example_placeholders(repo)

    assert "z/.env.example" in str(excinfo.value)


@pytest.mark.parametrize(
    "line",
    [
        'CONTACT="admin@example.com"',
        'DOCS="https://example.org/setup"',
        'UPPER="ADMIN@EXAMPLE.COM"',
    ],
)
def test_allows_every_reserved_example_domain(tmp_path: Path, line: str) -> None:
    """RFC 2606 reserves .com/.net/.org; only .net was covered, in one case."""
    repo = _git_repo(tmp_path, {"stack/.env.example": line + "\n"})
    check_env_example_placeholders(repo)


@pytest.mark.parametrize("line", ["TOKEN=<unterminated", "TOKEN=unopened>"])
def test_half_a_placeholder_is_not_a_placeholder(tmp_path: Path, line: str) -> None:
    """`<FOO>` needs both delimiters -- either one alone must still be flagged."""
    repo = _git_repo(tmp_path, {"stack/.env.example": line + "\n"})
    with pytest.raises(click.ClickException):
        check_env_example_placeholders(repo)


def test_success_summary_reports_how_many_examples_were_checked(tmp_path: Path, capsys) -> None:
    """The count is the operator's only evidence the check had any scope."""
    repo = _git_repo(
        tmp_path,
        {"a/.env.example": "TOKEN=<X>\n", "b/.env.example": "PORT=80\n", "c/notes.md": "hi\n"},
    )
    check_env_example_placeholders(repo)

    assert "2 .env.example file(s)" in capsys.readouterr().out
