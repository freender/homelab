from __future__ import annotations

import importlib.util
import os
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
