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
from homelab.leakcheck import _registrable

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
    # Whole-line, not a substring match: `in` alone still passes if the marker
    # is wrapped in something that itself leaks detail.
    assert "c.yml: external host <redacted>" in message
    assert UNLISTED_HOST not in message


def test_this_test_file_does_not_trip_the_checker(tmp_path: Path) -> None:
    """Guards the fixture-vs-scanner collision that `--others` exposed at commit time."""
    repo = _git_repo(tmp_path, {"test_leak_check.py": Path(__file__).read_text(encoding="utf-8")})
    check_public_repo_leaks(repo)


def test_finding_names_the_file_and_the_host(tmp_path: Path, monkeypatch) -> None:
    """A finding has to be actionable: which file, and which host.

    Without this, the path and the host could both be dropped from the message
    and every existing test would still pass -- they only assert the *kind* of
    finding. `CI` is cleared because the suite itself runs in CI, where the host
    is redacted by design.
    """
    monkeypatch.delenv("CI", raising=False)
    repo = _git_repo(tmp_path, {"deep/nested.yml": f"url: {UNLISTED_URL}\n"})

    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)

    message = str(excinfo.value)
    assert "deep/nested.yml" in message
    assert UNLISTED_HOST in message, "local runs must show the host, not redact it"


def test_an_unreadable_file_does_not_abandon_the_scan(tmp_path: Path) -> None:
    """A binary file is skipped, not treated as the end of the file list.

    Named so the undecodable file sorts first in `git ls-files` output, which is
    what makes this deterministic: if the skip aborted the loop instead of
    continuing, the leak in the later file would go unreported.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a-binary.bin").write_bytes(b"\xff\xfe\x00\x80 not utf-8 \xc3\x28")
    (repo / "z-leak.yml").write_text(f"url: {UNLISTED_URL}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)

    assert "z-leak.yml" in str(excinfo.value)


def test_a_skipped_host_does_not_abandon_the_rest_of_the_file(tmp_path: Path) -> None:
    """Each allowed-host rule must skip that host only, not stop scanning.

    One file, every skip reason in turn, and the leak last -- so any `continue`
    turning into a `break` loses the finding. Relies on `_external_url_hosts`
    preserving first-seen order.
    """
    text = "\n".join(
        [
            "vendor https://github.com/x",
            "internal https://xur.freender.internal:8007",
            "ip http://10.0.0.20:9100/metrics",
            "single http://localhost:8428",
            f"leak {UNLISTED_URL}",
        ]
    )
    repo = _git_repo(tmp_path, {"mixed.yml": text})

    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)

    assert "external host" in str(excinfo.value)


def test_trailing_dot_host_is_normalised_before_the_vendor_check(tmp_path: Path) -> None:
    """A trailing dot is a legal FQDN spelling, and must not defeat either rule.

    Without the strip the registrable domain of `github.com.` comes out as
    `com.`, which is in no allow-list; and the effective TLD of
    `xur.freender.internal.` comes out as the empty string rather than
    `internal`. Both would report an ordinary allowed URL as a leak.
    """
    repo = _git_repo(
        tmp_path,
        {
            "a.md": "see https://github.com./x and https://pypi.org./y",
            "b.conf": "target https://xur.freender.internal.:8007",
        },
    )
    check_public_repo_leaks(repo)


def test_success_summary_reports_how_many_files_were_scanned(tmp_path: Path, capsys) -> None:
    """The count is the only evidence the operator gets that the scan had scope."""
    repo = _git_repo(tmp_path, {"a.md": "clean", "b.md": "also clean"})
    check_public_repo_leaks(repo)

    assert "2 tracked file(s)" in capsys.readouterr().out


def test_redaction_hint_appears_only_when_redacting(tmp_path: Path, monkeypatch) -> None:
    """In CI the finding is useless without telling the reader where to look."""
    repo = _git_repo(tmp_path, {"c.yml": f"url: {UNLISTED_URL}\n"})

    monkeypatch.setenv("CI", "true")
    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)
    assert "Re-run locally" in str(excinfo.value)

    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)
    assert "Re-run locally" not in str(excinfo.value)


def test_configured_domains_accept_comma_and_space_separated_lists(
    tmp_path: Path, monkeypatch
) -> None:
    """Both separators are documented as supported; each needs its own evidence.

    One domain per file, because a finding deliberately does not name the domain
    it matched -- so two hits in one file would dedupe to a single line and prove
    nothing about the second entry parsing.
    """
    first, second = "route-one-example.test", "route-two-example.test"
    repo = _git_repo(tmp_path, {"a.md": f"see {first}\n", "b.md": f"see {second}\n"})

    for raw in (f"{first},{second}", f"{first} {second}"):
        monkeypatch.setenv("HOMELAB_LEAK_DOMAINS", raw)
        with pytest.raises(click.ClickException) as excinfo:
            check_public_repo_leaks(repo)
        message = str(excinfo.value)
        assert "a.md: banned domain" in message, f"first entry parses from {raw!r}"
        assert "b.md: banned domain" in message, f"second entry parses from {raw!r}"


def test_configured_domains_fall_back_to_the_config_file(tmp_path: Path, monkeypatch) -> None:
    """The out-of-band file is the non-CI path, and nothing covered its location."""
    monkeypatch.delenv("HOMELAB_LEAK_DOMAINS", raising=False)
    home = tmp_path / "home"
    config = home / ".config" / "homelab" / "leak-domains"
    config.parent.mkdir(parents=True)
    config.write_text("route-from-file-example.test\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    repo = _git_repo(tmp_path / "repo", {"notes.md": "see route-from-file-example.test\n"})

    with pytest.raises(click.ClickException) as excinfo:
        check_public_repo_leaks(repo)
    assert "banned domain" in str(excinfo.value)


def test_registrable_lowercases_a_single_label_host() -> None:
    """The `len(parts) < 2` branch is unreachable from the URL path, so test it directly."""
    assert _registrable("LOCALHOST") == "localhost"
    assert _registrable("a.b.Example.NET") == "example.net"
    assert _registrable(".github.com.") == "github.com"
