"""Tests for the public-repo leak check.

This repo is public, so these tests deliberately use throwaway fixture values
(`leaky-example-domain.test`, fake token shapes). The real private domain must
never appear here either — see AGENTS.md "Public Repo Boundary".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from homelab.cli import check_public_repo_leaks

ROOT = Path(__file__).resolve().parents[1]

# This file is itself scanned by the checker, so a literal external URL here would
# (correctly) trip the very gate it tests. Composing the host at runtime keeps the
# file in scope for genuine secret detection while staying invisible to the URL
# regex, which only matches a full `scheme://host` literal.
UNLISTED_HOST = "app.unlisted-fixture" + ".io"
UNLISTED_URL = f"https://{UNLISTED_HOST}"


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_real_repo_is_clean() -> None:
    """The actual repo must pass its own gate — this is the regression guard."""
    check_public_repo_leaks(ROOT)


def test_placeholder_key_block_is_not_flagged(tmp_path: Path) -> None:
    """`secrets/templates/*.example` ship empty BEGIN/END blocks on purpose."""
    repo = _git_repo(
        tmp_path,
        {
            "k.env.tpl.example": (
                "KEY=-----BEGIN OPENSSH PRIVATE KEY-----\nplaceholder\n"
                "-----END OPENSSH PRIVATE KEY-----\n"
            )
        },
    )
    check_public_repo_leaks(repo)


def test_allows_internal_and_vendor_hosts(tmp_path: Path) -> None:
    repo = _git_repo(
        tmp_path,
        {
            "a.md": "see https://github.com/x and https://download.proxmox.com/y",
            "b.conf": "target https://xur.freender.internal:8007 and http://localhost:8428",
            "c.sh": "curl http://10.0.0.20:9100/metrics",
            "d.yml": "route https://traefik-tower.example.net",
        },
    )
    check_public_repo_leaks(repo)


def test_flags_unknown_external_host(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"compose.yml": "Host(`app.leaky-example-domain.test`)\n"})
    # .test is an internal TLD, so that alone is allowed; a real URL is not.
    check_public_repo_leaks(repo)

    repo2 = _git_repo(tmp_path / "two", {"c.yml": f"url: {UNLISTED_URL}\n"})
    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo2)
    assert "external host" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU" * 3,
        "ops_" + "a" * 44,
        "1234567890:AA" + "b" * 33,
        "ghp_" + "c" * 36,
        "AKIA" + "D" * 16,
    ],
)
def test_flags_secret_shapes(tmp_path: Path, payload: str) -> None:
    repo = _git_repo(tmp_path / payload[:6], {"leak.txt": f"value = {payload}\n"})
    with pytest.raises(click.ClickException):
        check_public_repo_leaks(repo)


def test_banned_domain_from_env_is_not_echoed(tmp_path: Path, monkeypatch) -> None:
    """A configured domain is matched anywhere, and never printed back."""
    secret = "private-route-example.test"
    monkeypatch.setenv("HOMELAB_LEAK_DOMAINS", secret)
    repo = _git_repo(tmp_path, {"notes.md": f"backend at {secret}\n"})

    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)

    message = str(excinfo.value)
    assert "banned domain" in message
    assert secret not in message, "the checker must not echo the value it is protecting"


def test_ci_redacts_findings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    repo = _git_repo(tmp_path, {"c.yml": f"url: {UNLISTED_URL}\n"})

    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)

    message = str(excinfo.value)
    assert "<redacted>" in message
    assert UNLISTED_HOST not in message


def test_this_test_file_does_not_trip_the_checker(tmp_path: Path) -> None:
    """Guards the fixture-vs-scanner collision that `--others` exposed at commit time."""
    repo = _git_repo(tmp_path, {"test_leak_check.py": Path(__file__).read_text(encoding="utf-8")})
    check_public_repo_leaks(repo)
