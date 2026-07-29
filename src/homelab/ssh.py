from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from fabric import Connection
from invoke.exceptions import UnexpectedExit

from homelab.hosts import HostLookupError, default_registry


class HostConnection:
    def __init__(self, host: str, user: str | None = None, hostname: str | None = None) -> None:
        self.host = host
        resolved_user = user
        resolved_hostname = hostname

        if resolved_user is None or resolved_hostname is None:
            try:
                registry = default_registry(Path.cwd())
                resolved_user = resolved_user or str(registry.get(host, "config.user"))
                resolved_hostname = resolved_hostname or str(registry.get(host, "config.hostname"))
            except HostLookupError:
                pass

        connect_target = resolved_hostname or host
        self.connection = Connection(connect_target, user=resolved_user)

    def remote_diff(self, local_file: Path, remote_path: str) -> tuple[int, str]:
        if offline_mode():
            return offline_diff(remote_path)

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            temp_path = Path(handle.name)

        try:
            try:
                self.connection.get(remote_path, str(temp_path))
            except UnexpectedExit:
                return 2, f"[NEW] {remote_path}"
            except FileNotFoundError:
                return 2, f"[NEW] {remote_path}"
            except PermissionError:
                return 0, f"[?] {remote_path} (not diffable: permission denied)"
            except OSError as exc:
                if exc.errno == 13:  # EACCES — root-only file
                    return 0, f"[?] {remote_path} (not diffable: permission denied)"
                raise

            if local_file.read_bytes() == temp_path.read_bytes():
                return 0, f"[=] {remote_path} (no changes)"

            return 1, f"[~] {remote_path}"
        finally:
            temp_path.unlink(missing_ok=True)

    def prepare_remote_dir(self, remote_root: str, *subdirs: str) -> None:
        directories = [shlex.quote(remote_root)]
        directories.extend(shlex.quote(f"{remote_root}/{subdir}") for subdir in subdirs)
        joined = " ".join(directories)
        self.connection.run(
            f"rm -rf {shlex.quote(remote_root)} && mkdir -p {joined}", hide=True
        )

    def upload(self, local_path: Path, remote_path: str) -> None:
        if local_path.is_dir():
            self.upload_dir(local_path, remote_path)
            return
        self.connection.put(str(local_path), remote=remote_path)

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        remote_root = remote_dir.rstrip("/")
        self.connection.run(f"mkdir -p {shlex.quote(remote_root)}", hide=True)
        for path in sorted(local_dir.rglob("*")):
            relative_path = path.relative_to(local_dir)
            target_path = f"{remote_root}/{relative_path.as_posix()}"
            if path.is_dir():
                self.connection.run(f"mkdir -p {shlex.quote(target_path)}", hide=True)
            else:
                parent = target_path.rsplit("/", 1)[0]
                self.connection.run(f"mkdir -p {shlex.quote(parent)}", hide=True)
                self.connection.put(str(path), remote=target_path)

    def upload_shared_libs(self, root: Path, remote_root: str) -> None:
        self.upload(root / "lib" / "print.sh", f"{remote_root}/lib/print.sh")
        self.upload(root / "lib" / "utils.sh", f"{remote_root}/lib/utils.sh")

    def upload_paths(self, paths: list[tuple[Path, str]]) -> None:
        for local_path, remote_path in paths:
            self.upload(local_path, remote_path)

    def run_remote_installer(
        self,
        remote_dir: str,
        installer: str,
        *args: str,
        env: dict[str, str] | None = None,
        require_root: bool = False,
        interpreter: str | None = None,
    ) -> None:
        # Everything interpolated here runs as root on the target. Double-quoting is not
        # enough: a value containing $, `, \ or " would be shell-expanded or would break
        # out of the quoting. shlex.quote makes each piece a literal.
        joined_args = " ".join(shlex.quote(arg) for arg in args)
        quoted_installer = shlex.quote(installer)
        command = f"cd {shlex.quote(remote_dir)} && chmod +x {quoted_installer}"
        if require_root:
            command += (
                ' && if [ "$(id -u)" -ne 0 ]; then '
                'echo "Error: deploy requires root SSH user" >&2; exit 1; fi'
            )
        installer_command = quoted_installer
        if interpreter:
            installer_command = f"{interpreter} {quoted_installer}"
        if env:
            exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
            command += f" && env {exports} {installer_command} {joined_args}"
        else:
            command += f" && {installer_command} {joined_args}"
        self.connection.run(command, pty=False)


def diff_many(connection: HostConnection, file_pairs: list[tuple[Path, str]]) -> list[str]:
    messages: list[str] = []
    for local_path, remote_path in file_pairs:
        _, message = connection.remote_diff(local_path, remote_path)
        messages.append(message)
    return messages


def offline_mode() -> bool:
    return os.environ.get("HOMELAB_OFFLINE", "").lower() in {"1", "true", "yes"}


def offline_diff(remote_path: str) -> tuple[int, str]:
    return 3, f"[?] {remote_path} (offline validation; remote diff skipped)"


def build_files(build_dir: Path) -> list[str]:
    return [
        str(file_path.relative_to(build_dir))
        for file_path in sorted(build_dir.rglob("*"))
        if file_path.is_file()
    ]



