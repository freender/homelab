from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .output import print_action, print_ok, print_sub, print_warn
from .ssh import HostConnection


def prepare_build_dir(build_dir: Path) -> None:
    previous_dir = build_dir.with_name(f"{build_dir.name}.prev")
    if previous_dir.exists():
        shutil.rmtree(previous_dir)
    if build_dir.exists():
        build_dir.rename(previous_dir)
    build_dir.mkdir(parents=True, exist_ok=True)


def force_env(force: bool) -> dict[str, str]:
    return {"FORCE_UPDATE": "true" if force else "false"}


# Where `upload_shared_libs(include_python=True)` puts `homelab_install`, relative
# to a module's remote staging root. Stated once here; `ssh.py` builds the same
# path from its own two components and nothing else may hardcode it.
PYTHON_LIB_SUBDIR = "lib/py"


def is_python_installer(installer: str) -> bool:
    """Whether a staged installer path runs under `python3` rather than bash.

    The suffix is the whole signal, and it is deliberately the only one: a module
    declares `scripts/install.py` and everything downstream — uploading
    `lib/py/`, setting `PYTHONPATH` — follows from that, with no second flag for
    a module to set inconsistently.
    """
    return installer.endswith(".py")


def python_lib_env(remote_root: str, env: dict[str, str] | None) -> dict[str, str]:
    """Add the staged `lib/py` to `PYTHONPATH` without dropping a caller's own.

    Prepending rather than overwriting keeps `homelab_install` winning against a
    same-named module elsewhere on the path, which is the failure that would be
    hardest to diagnose from a deploy log.
    """
    lib_path = f"{remote_root}/{PYTHON_LIB_SUBDIR}"
    merged = dict(env or {})
    existing = merged.get("PYTHONPATH")
    merged["PYTHONPATH"] = f"{lib_path}:{existing}" if existing else lib_path
    return merged


@dataclass
class DeploySession:
    module: str
    failed_hosts: list[str] = field(default_factory=list)

    def run(self, deploy_host: Callable[[str], None], hosts: list[str]) -> None:
        print_action(f"Deploying {self.module}")
        print_sub(f"Hosts: {' '.join(hosts)}")
        print()

        for host in hosts:
            print_action(f"Deploying to {host}...")
            try:
                deploy_host(host)
            except Exception as exc:
                print_warn(f"Failed to deploy to {host}: {exc}")
                self.failed_hosts.append(host)
            else:
                print_ok(f"Deployed to {host}")
            print()

    def finish(self) -> bool:
        print_action("Deployment complete!")
        if self.failed_hosts:
            print()
            print_warn(f"Failed hosts: {' '.join(self.failed_hosts)}")
            return False
        return True


def stage_and_run_remote_installer(
    root: Path,
    connection: HostConnection,
    remote_root: str,
    upload_paths: list[tuple[Path, str]],
    installer: str,
    *args: str,
    env: dict[str, str] | None = None,
    require_root: bool = False,
    interpreter: str | None = None,
    remote_subdirs: tuple[str, ...] = ("build", "lib"),
) -> None:
    python_installer = is_python_installer(installer)

    print_sub("Staging bundle...")
    connection.prepare_remote_dir(remote_root, *remote_subdirs)
    connection.upload_paths(upload_paths)
    connection.upload_shared_libs(root, remote_root, include_python=python_installer)
    if python_installer:
        env = python_lib_env(remote_root, env)

    print_sub("Running installer...")
    connection.run_remote_installer(
        remote_root,
        installer,
        *args,
        env=env,
        require_root=require_root,
        interpreter=interpreter,
    )
