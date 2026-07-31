from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml

from . import op_secrets
from .deploy import DeploySession
from .hosts import HostLookupError, default_registry, validate_hosts_data
from .modules import MODULES, ordered_modules
from .output import print_action, print_error, print_header, print_ok, print_sub, print_warn
from .ssh import offline_mode


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_feature_registry(root: Path) -> None:
    """Cross-check hosts.conf feature names against the module registry, both ways.

    Nothing previously connected the two, so a feature block could name a module that
    does not exist (a typo silently deploys nothing) and a registered module could be
    enabled by no host at all (dead code that still passes CI).
    """
    registry = default_registry(root)
    declared = registry.declared_features()
    registered = set(MODULES)

    unknown = sorted(declared - registered)
    if unknown:
        raise click.ClickException(
            "hosts.conf declares feature(s) with no matching module: "
            f"{', '.join(unknown)}. Registered modules: {', '.join(sorted(registered))}"
        )

    # Not fatal: a module can legitimately sit in the registry between hosts. But it
    # deploys nowhere and is never exercised, so it must not be silent.
    orphaned = sorted(module for module in registered if not registry.list_hosts(feature=module))
    for module in orphaned:
        print_warn(f"module '{module}' is registered but no host enables it; it deploys nowhere")

    print_ok(f"{len(declared)} feature(s) map to registered modules")


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
        rel = path.relative_to(root)

        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{rel}: {label}")

        lowered = text.lower()
        for domain in banned:
            if domain in lowered:
                findings.append(f"{rel}: banned domain")

        for host in set(_URL_HOST.findall(text)):
            host = host.lower().strip(".")
            if "." not in host or host == "localhost":
                continue
            if re.fullmatch(r"[\d.]+", host):
                continue  # bare IP literal
            if host.rsplit(".", 1)[-1] in _INTERNAL_TLDS:
                continue
            if _registrable(host) in _VENDOR_DOMAINS:
                continue
            findings.append(f"{rel}: external host {_redact(host)}")

    if findings:
        raise click.ClickException(
            "public-repo leak check failed (see AGENTS.md 'Public Repo Boundary'):\n  "
            + "\n  ".join(sorted(set(findings)))
            + "\n\nUse an example.net placeholder for route hosts, or add a genuine"
            " vendor domain to _VENDOR_DOMAINS in src/homelab/cli.py."
            + ("\nRe-run locally for unredacted detail." if _redacting() else "")
        )

    print_ok(f"{len(tracked)} tracked file(s) clean of secrets and external hosts")


@click.group()
def main() -> None:
    """Homelab deployment CLI."""


@main.group()
def hosts() -> None:
    """Host inventory commands."""


@hosts.command("list")
@click.option("--feature", help="Filter by feature name.")
def list_hosts(feature: str | None) -> None:
    registry = default_registry(repo_root())
    for host in registry.list_hosts(feature=feature):
        click.echo(host)


@main.command()
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes only.")
@click.option("--force", is_flag=True, default=False, help="Force remote updates.")
@click.argument("module")
@click.argument("host")
def deploy(dry_run: bool, force: bool, module: str, host: str) -> None:
    if module == "all":
        failed_modules: list[str] = []
        print_action(f"Deploying homelab to: {host}")
        print()
        for module_name in ordered_modules():
            exit_code = execute_module(module_name, host, dry_run, force)
            if exit_code != 0:
                failed_modules.append(module_name)
        print()
        if failed_modules:
            # Do NOT print "Deploy complete!" here: a partial deploy is not a success,
            # and raising the ClickException (rather than only reading its exit_code)
            # is what actually surfaces which modules failed.
            raise click.ClickException(f"Failed modules: {' '.join(failed_modules)}")
        print_action("Deploy complete!")
        raise SystemExit(0)

    module_definition = MODULES.get(module)
    if module_definition is None:
        raise click.ClickException(f"Unknown or unported module: {module}")

    exit_code = execute_module(module, host, dry_run, force)
    raise SystemExit(exit_code)


@main.command()
def validate() -> None:
    # Validation is intentionally offline: no SSH, no op CLI calls.
    # Modules fall back to template `.example` siblings under secrets/templates/.
    os.environ.setdefault("HOMELAB_OFFLINE", "1")

    root = repo_root()
    print_header("Homelab Validation")

    print_action("Python")
    _run_command([sys.executable, "-m", "compileall", "src"], cwd=root)
    print_ok("Python sources compile")

    # Ruff and pytest both gate CI. Running them here is what makes `./validate` an
    # honest pre-PR check: without them you could follow the AGENTS.md checklist,
    # see a green validate, push, and still land a red build.
    if _module_available("ruff"):
        print_action("Ruff")
        _run_command([sys.executable, "-m", "ruff", "check", "src", "tests"], cwd=root)
        print_ok("Ruff passed")
    else:
        print_warn("ruff not installed; skipping Python lint (CI will still run it)")

    if _module_available("pytest"):
        print_action("Pytest")
        _run_command([sys.executable, "-m", "pytest", "-q", "tests"], cwd=root)
        print_ok("Tests passed")
    else:
        print_warn("pytest not installed; skipping tests (CI will still run them)")

    print_action("YAML Syntax")
    with (root / "hosts.conf").open("r", encoding="utf-8") as handle:
        hosts_data = yaml.safe_load(handle)
    validate_hosts_data({} if hosts_data is None else hosts_data, root / "hosts.conf")
    print_ok("hosts.conf valid")

    print_action("Inventory")
    check_feature_registry(root)

    print_action("Leak Check")
    check_public_repo_leaks(root)

    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        print_action("ShellCheck")
        shell_scripts = sorted(str(path) for path in root.rglob("*.sh") if ".bin" not in path.parts)
        if shell_scripts:
            _run_command([shellcheck, "-S", "warning", *shell_scripts], cwd=root)
        print_ok("ShellCheck passed")
    else:
        print_warn("shellcheck not installed; skipping shell lint")

    print_action("Dry-run Modules")
    if offline_mode():
        print_sub("Offline mode enabled; remote SSH diffs are skipped")
    failed_modules: list[str] = []
    for module_name in ordered_modules():
        print_sub(module_name)
        exit_code = execute_module(module_name, "all", True, False)
        if exit_code == 0:
            print_ok("OK")
        else:
            print_warn("Issues")
            failed_modules.append(module_name)

    if failed_modules:
        raise click.ClickException(f"dry-run failures: {' '.join(failed_modules)}")

    print_header("Validation Complete")


def _run_command(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise click.ClickException(f"command failed: {' '.join(command)}")


def execute_module(module_name: str, host: str, dry_run: bool, force: bool) -> int:
    module_definition = MODULES[module_name]
    session = DeploySession(module_definition.name)
    try:
        return module_definition.deploy(repo_root(), host, dry_run, force, session)
    except (HostLookupError, ValueError) as exc:
        print_error(f"{module_name}: {exc}")
        return 1


@main.group()
def secrets() -> None:
    """1Password-backed secret management."""


@secrets.command("doctor")
@click.argument("names", nargs=-1)
def secrets_doctor(names: tuple[str, ...]) -> None:
    """Verify catalog entries resolve via `op inject`. Names are not printed."""
    root = repo_root()
    print_header("Homelab Secrets Doctor")
    if offline_mode():
        print_sub("Offline mode: checking offline example fallbacks only")
    exit_code = op_secrets.doctor(root, names if names else None)
    raise SystemExit(exit_code)


@secrets.command("list")
def secrets_list() -> None:
    """List all secret names defined in secrets/catalog.yml."""
    root = repo_root()
    try:
        for name in op_secrets.list_secret_names(root):
            click.echo(name)
    except op_secrets.OpSecretsError as exc:
        raise click.ClickException(str(exc)) from exc


@secrets.command("render")
def secrets_render() -> None:
    """Materialize all secrets into tmpfs and report the path.

    With the default 24-hour cache enabled, the reported path is the shared
    tmpfs cache. Use `homelab secrets cache-clear` to remove it early.
    Use this for manual inspection only; never copy contents elsewhere.
    """
    root = repo_root()
    print_header("Render Secrets (ephemeral)")
    try:
        path = op_secrets.render_all(root)
    except op_secrets.OpSecretsError as exc:
        raise click.ClickException(str(exc)) from exc
    print_sub(f"Rendered to: {path}")
    print_warn("Secrets stay in tmpfs until cache expiry, cache-clear, or reboot.")


@secrets.command("cache-status")
def secrets_cache_status() -> None:
    """Show the shared tmpfs secret cache path, TTL, and file ages."""
    try:
        info = op_secrets.cache_info()
    except op_secrets.OpSecretsError as exc:
        raise click.ClickException(str(exc)) from exc
    print_header("Homelab Secrets Cache")
    print_sub(f"Path: {info['path']}")
    print_sub(f"TTL: {info['ttl_seconds']} seconds")
    files = info["files"]
    if not files:
        print_sub("Files: none")
        return
    for file_info in files:
        print_sub(
            f"{file_info['name']} age={file_info['age_seconds']}s "
            f"size={file_info['size']}B"
        )


@secrets.command("cache-clear")
def secrets_cache_clear() -> None:
    """Shred and remove the shared tmpfs secret cache."""
    try:
        op_secrets.clear_cache()
    except op_secrets.OpSecretsError as exc:
        raise click.ClickException(str(exc)) from exc
    print_ok("secret cache cleared")


@secrets.command("bootstrap")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing fields on items already present in 1Password.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be created/updated without writing to 1Password.",
)
@click.argument("names", nargs=-1)
def secrets_bootstrap(force: bool, dry_run: bool, names: tuple[str, ...]) -> None:
    """One-time migration: create 1Password items from legacy secrets/*.env files.

    Requires the service-account token (or session) to have rw on the vault.
    After bootstrap, downgrade the service account back to read-only and
    run `homelab secrets purge-local`.
    """
    root = repo_root()
    print_header("Homelab Secrets Bootstrap")
    if dry_run:
        print_sub("Dry-run: no writes will be sent to 1Password.")
    if force:
        print_sub("Force enabled: existing items will be overwritten.")
    exit_code = op_secrets.bootstrap(
        root,
        names if names else None,
        force=force,
        dry_run=dry_run,
    )
    raise SystemExit(exit_code)


@secrets.command("purge-local")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation.")
def secrets_purge_local(yes: bool) -> None:
    """Shred and remove plaintext secrets/*.env files on this machine.

    Run only AFTER `homelab secrets doctor` confirms every catalog entry
    resolves from 1Password. Examples and templates are kept.
    """
    root = repo_root()
    secrets_dir = root / "secrets"
    candidates = sorted(
        path
        for path in secrets_dir.glob("*.env")
        if path.is_file() and not path.name.endswith(".example")
    )
    if not candidates:
        print_action("No plaintext .env files under secrets/ to purge.")
        return

    print_action(f"Found {len(candidates)} plaintext file(s) under {secrets_dir}:")
    for path in candidates:
        print_sub(path.name)

    if not yes:
        click.confirm(
            "Shred and remove these files? Confirm 1Password has every value first.",
            abort=True,
        )

    shred = shutil.which("shred")
    for path in candidates:
        try:
            if shred:
                subprocess.run(
                    [shred, "-u", "-n", "1", str(path)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if path.exists():
                path.unlink()
            print_ok(f"removed {path.name}")
        except OSError as exc:
            print_error(f"failed to remove {path}: {exc}")


if __name__ == "__main__":
    main()
