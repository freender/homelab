"""The mechanical half of AGENTS.md's "Public Repo Boundary".

Split out of `cli.py` so it can be mutation-tested on its own: `[tool.mutmut]`
scopes by file glob and has no function-level granularity, so the only way to
put these checks under the ratchet without also mutating every click wrapper in
`cli.py` is for them to live in their own module.

`cli.py` imports `check_public_repo_leaks` and `check_env_example_placeholders`
back, so both stay importable as `homelab.cli.<name>` for the existing tests.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import click

from .output import print_ok, print_warn

# --- Public repo leak check -------------------------------------------------
#
# This repo is public (AGENTS.md "Public Repo Boundary"). These checks are the
# mechanical half of that rule.
#
# Note the deliberate asymmetry: the private domain is NOT hardcoded here,
# because writing it into a public file is the very leak we are preventing.
# Instead we flag any externally routable URL host that is not a known vendor,
# which catches the domain without naming it (and catches future ones too).
# Exact strings can additionally be supplied out-of-band via
# HOMELAB_LEAK_DOMAINS or ~/.config/homelab/leak-domains (CI: a repo secret).

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Require real base64 key material after the header: `.tpl.example` files
    # legitimately ship an empty BEGIN/END block around the word "placeholder".
    (
        "private key block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]*?[A-Za-z0-9+/]{40,}"),
    ),
    ("1Password service-account token", re.compile(r"\bops_[A-Za-z0-9]{40,}")),
    ("Telegram bot token", re.compile(r"\b\d{9,10}:AA[A-Za-z0-9_-]{32,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)

_URL_HOST = re.compile(r"https?://([A-Za-z0-9._~-]+)")

# Hostnames under these TLDs never leave the homelab, so they are safe to commit.
_INTERNAL_TLDS = frozenset({"internal", "local", "invalid", "localdomain", "lan", "test"})

# Registrable domains we intentionally reference (package repos, APIs, docs).
_VENDOR_DOMAINS = frozenset(
    {
        "astral.sh",
        "debian.org",
        "docker.com",
        "example.com",
        "example.net",
        "example.org",
        "github.com",
        "githubusercontent.com",
        "grafana.com",
        "kernel.org",
        "microsoft.com",
        "opencode.ai",
        "openssh.com",
        "proxmox.com",
        "pypi.org",
        "python.org",
        "plex.tv",
        "telegram.org",
        "ubuntu.com",
    }
)


def _redacting() -> bool:
    """CI logs on a public repo are themselves public, so never echo findings there."""
    return bool(os.environ.get("CI"))


def _redact(value: str) -> str:
    """Show the offending host locally; withhold it from public CI output."""
    return "<redacted>" if _redacting() else f"'{value}'"


def _registrable(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _configured_leak_domains() -> list[str]:
    """Extra literal strings to ban, supplied out-of-band so they stay unpublished."""
    raw = os.environ.get("HOMELAB_LEAK_DOMAINS", "")
    if not raw:
        config = Path.home() / ".config" / "homelab" / "leak-domains"
        if config.is_file():
            raw = config.read_text(encoding="utf-8")
    separators = str.maketrans({",": "\n", " ": "\n"})
    return [line.strip().lower() for line in raw.translate(separators).splitlines() if line.strip()]


def _tracked_files(root: Path) -> list[Path]:
    """Files that are, or are about to be, published.

    `--others --exclude-standard` includes untracked-but-not-ignored files. Without
    them the check only sees committed content, so `./validate` passes on a new file
    and then fails the moment it is committed -- the gate would change scope exactly
    when it stops being useful. Ignored files stay out: they are never published.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [root / name for name in result.stdout.split("\0") if name]


def _external_url_hosts(text: str) -> list[str]:
    """URL hosts in `text` that are genuinely externally routable.

    Everything skipped here is unroutable or deliberately public per AGENTS.md:
    internal TLDs, bare IP literals, localhost, and the vendor allow-list.
    """
    external: list[str] = []
    for raw_host in set(_URL_HOST.findall(text)):
        host = raw_host.lower().strip(".")
        if "." not in host or host == "localhost":
            continue
        if re.fullmatch(r"[\d.]+", host):
            continue  # bare IP literal
        if host.rsplit(".", 1)[-1] in _INTERNAL_TLDS:
            continue
        if _registrable(host) in _VENDOR_DOMAINS:
            continue
        external.append(host)
    return external


def _scan_for_leaks(rel: Path, text: str, banned: list[str]) -> list[str]:
    """Every leak one file's contents contains, labelled by kind."""
    lowered = text.lower()
    findings = [f"{rel}: {label}" for label, pattern in _SECRET_PATTERNS if pattern.search(text)]
    findings.extend(f"{rel}: banned domain" for domain in banned if domain in lowered)
    findings.extend(
        f"{rel}: external host {_redact(host)}" for host in _external_url_hosts(text)
    )
    return findings


def check_public_repo_leaks(root: Path) -> None:
    """Fail the build on anything that must never be published from this repo."""
    banned = _configured_leak_domains()
    findings: list[str] = []

    tracked = _tracked_files(root)
    if not tracked:
        print_warn("git not available; skipping leak check")
        return

    for path in tracked:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable; nothing scannable
        findings.extend(_scan_for_leaks(path.relative_to(root), text, banned))

    if findings:
        raise click.ClickException(
            "public-repo leak check failed (see AGENTS.md 'Public Repo Boundary'):\n  "
            + "\n  ".join(sorted(set(findings)))
            + "\n\nUse an example.net placeholder for route hosts, or add a genuine"
            " vendor domain to _VENDOR_DOMAINS in src/homelab/leakcheck.py."
            + ("\nRe-run locally for unredacted detail." if _redacting() else "")
        )

    print_ok(f"{len(tracked)} tracked file(s) clean of secrets and external hosts")


# --- .env.example placeholder check ------------------------------------------
#
# `.env.example` files document the keys a host-local `.env` needs without
# shipping real values (AGENTS.md "Public Repo Boundary": ".env.example ...
# placeholders are allowed for offline validation"). Nothing enforced that
# promise -- someone pasting a real token into one during a copy/paste from a
# live host would only be caught by check_public_repo_leaks if the value
# happened to match a known secret *shape*. This check is stricter: every
# assigned value must look like a placeholder, an allow-listed literal
# default, or empty; anything else fails, whether or not it looks secret-shaped.

_ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_DURATION_LITERAL = re.compile(r"\d+(ms|[smhdw])$", re.IGNORECASE)
_JINJA_PLACEHOLDER = re.compile(r"^\{\{\s*[A-Za-z0-9_]+\s*\}\}$")
_XPLACEHOLDER = re.compile(r"^[xX][xX:-]*$")
_SAFE_LITERALS = frozenset({"true", "false", "info", "debug", "warn", "warning", "error"})


def _unquoted(raw: str) -> str:
    """Strip one matching pair of surrounding quotes, if present."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _has_placeholder_shape(value: str) -> bool:
    """Its *form* marks it as a stand-in: <FOO>, {{ FOO }}, xxxx, xx:xx:xx."""
    if value.startswith("<") and value.endswith(">"):
        return True
    return bool(_JINJA_PLACEHOLDER.match(value) or _XPLACEHOLDER.match(value))


def _is_non_secret_literal(value: str) -> bool:
    """A real value that cannot be a credential: number, duration, path, socket."""
    if re.fullmatch(r"-?\d+", value):
        return True
    if _DURATION_LITERAL.fullmatch(value):
        return True
    return value.startswith("/") or value.startswith("unix://")


def _is_documented_stand_in(value: str) -> bool:
    """It says 'fill me in', or points at an RFC 2606 reserved example domain."""
    lowered = value.lower()
    if lowered.startswith("replace-with") or "changeme" in lowered:
        return True
    return any(domain in lowered for domain in ("example.com", "example.net", "example.org"))


def _is_placeholder_value(raw: str) -> bool:
    """Whether an `.env.example` value looks like a placeholder, not a real one.

    The three predicates are independent and order between them does not matter;
    each answers a different question about the same value. Only the empty check
    has to come first, since the others assume a non-empty string.
    """
    value = _unquoted(raw)
    if not value:
        return True
    if value.lower() in _SAFE_LITERALS:
        return True
    return (
        _has_placeholder_shape(value)
        or _is_non_secret_literal(value)
        or _is_documented_stand_in(value)
    )


def _non_placeholder_assignments(rel: Path, text: str) -> list[str]:
    """Assignments in one `.env.example` whose value does not look like a placeholder."""
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.match(stripped)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if not _is_placeholder_value(value):
            findings.append(f"{rel}:{lineno}: {key} is not a placeholder value")
    return findings


def check_env_example_placeholders(root: Path) -> None:
    """Fail the build if any `.env.example` assigns something other than a placeholder."""
    findings: list[str] = []
    examples = [path for path in _tracked_files(root) if path.name.endswith(".env.example")]

    for path in examples:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(_non_placeholder_assignments(path.relative_to(root), text))

    if findings:
        raise click.ClickException(
            ".env.example placeholder check failed -- these look like real values, not "
            "placeholders:\n  "
            + "\n  ".join(sorted(set(findings)))
            + "\n\nUse <PLACEHOLDER>, an empty value, or a genuinely non-secret literal "
            "(see _is_placeholder_value in src/homelab/leakcheck.py)."
        )

    print_ok(f"{len(examples)} .env.example file(s) contain placeholders only")
