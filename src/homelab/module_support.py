from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import op_secrets

if TYPE_CHECKING:
    from .deploy import DeploySession


@dataclass(frozen=True)
class FileSpec:
    build_name: str
    remote_path: str
    mode: str = "644"
    feature: str | None = None


@dataclass(frozen=True)
class HostArtifacts:
    build_dir: Path
    file_specs: tuple[FileSpec, ...]


def require_text(value: object, message: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(message)
    return text


def normalize_bool(value: object, default: bool, message: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(message)


def feature_paused(registry, host: str, feature: str, default: bool = False) -> bool:
    """Return whether a feature is paused for a host.

    `<feature>.paused` is a distinct knob from the host-level `deploy: false`
    (formerly `enabled: false`) targeting gate. `deploy: false` removes the host
    from a module's deploy targets entirely and never touches the running
    service; `paused: true` keeps the module deploying so it can actively stop
    and disable the managed units, and can be flipped back to resume.
    """
    return normalize_bool(
        registry.get(host, f"{feature}.paused", None),
        default,
        f"{feature}.paused must be true or false for {host}",
    )


def normalize_string_list(value: object, message: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(message)
    normalized = []
    for item in value:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def write_file_map(build_dir: Path, file_specs: tuple[FileSpec, ...]) -> None:
    lines = [f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in file_specs]
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")


def connection_for_host(root: Path, host: str):
    from .hosts import default_registry
    from .ssh import HostConnection

    registry = default_registry(root)
    return HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname", host)),
    )


@contextmanager
def tmpfs_secret_stage(prefix: str) -> Iterator[Path]:
    base = Path("/dev/shm")
    if not base.is_dir():
        raise RuntimeError("/dev/shm not available; cannot stage secrets safely")
    stage_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
    stage_dir.chmod(0o700)
    try:
        yield stage_dir
    finally:
        shred = shutil.which("shred")
        for file_path in sorted(stage_dir.rglob("*"), reverse=True):
            if not file_path.is_file():
                continue
            try:
                if shred:
                    subprocess.run(
                        [shred, "-u", "-n", "1", str(file_path)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    file_path.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(stage_dir, ignore_errors=True)


def copy_cached_secret(root: Path, secret_name: str, destination: Path) -> Path:
    source = op_secrets.secret_file(root, secret_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    return destination


def validate_secret_reference(root: Path, secret_name: str) -> None:
    catalog = op_secrets.load_catalog(root)
    entry = catalog.get(secret_name)
    if entry is None:
        raise op_secrets.OpSecretsError(
            f"unknown secret '{secret_name}' (not in {op_secrets.CATALOG_PATH})"
        )
    if op_secrets.offline_mode() and entry.example is None:
        raise op_secrets.OpSecretsError(
            f"offline mode: no example file for secret '{secret_name}'. "
            f"Create {entry.template}.example to support offline validation."
        )


def simple_root_installer_deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
    *,
    feature: str,
    remote_root: str,
    env_for_host: Callable[[str], dict[str, str]] | None = None,
    dry_run_details: Callable[[str], list[str]] | None = None,
) -> int:
    from .deploy import stage_and_run_remote_installer
    from .hosts import default_registry
    from .output import print_action, print_sub

    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature=feature)
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping {feature} (not applicable to {requested_host})")
        return 0

    installer = root / feature / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"Missing installer: {installer}")

    def deploy_host(host: str) -> None:
        connection = connection_for_host(root, host)
        env = env_for_host(host) if env_for_host else None
        if dry_run:
            print_action(f"[DRY-RUN] Would deploy {feature} to {host}")
            if dry_run_details:
                for detail in dry_run_details(host):
                    print_sub(detail)
            return
        stage_and_run_remote_installer(
            root,
            connection,
            remote_root,
            [(root / feature / "scripts", f"{remote_root}/scripts")],
            "scripts/install.sh",
            host,
            env=env,
            require_root=True,
            remote_subdirs=("lib",),
        )

    session.run(deploy_host, hosts)
    return 0 if session.finish() else 1
