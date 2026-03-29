from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml

from .deploy import DeploySession
from .hosts import HostLookupError, default_registry
from .modules import MODULES, ordered_modules
from .output import print_action, print_error, print_header, print_ok, print_sub, print_warn
from .ssh import offline_mode


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
        print_action("Deploy complete!")
        if failed_modules:
            message = f"Failed modules: {' '.join(failed_modules)}"
            raise SystemExit(click.ClickException(message).exit_code)
        raise SystemExit(0)

    module_definition = MODULES.get(module)
    if module_definition is None:
        raise click.ClickException(f"Unknown or unported module: {module}")

    exit_code = execute_module(module, host, dry_run, force)
    raise SystemExit(exit_code)


@main.command()
def validate() -> None:
    root = repo_root()
    print_header("Homelab Validation")

    print_action("Python")
    _run_command([sys.executable, "-m", "compileall", "src"], cwd=root)
    print_ok("Python sources compile")

    print_action("YAML Syntax")
    with (root / "hosts.conf").open("r", encoding="utf-8") as handle:
        yaml.safe_load(handle)
    print_ok("hosts.conf valid")

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


if __name__ == "__main__":
    main()
