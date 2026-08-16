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
