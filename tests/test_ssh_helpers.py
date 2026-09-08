from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from invoke.exceptions import UnexpectedExit

from homelab import ssh


class DummyConnection:
    """Stand-in for fabric.Connection used across ssh.py tests.

    `get_effect` controls what `.get()` does: None copies `get_contents` to the
    destination (simulating a successful fetch), an exception instance is raised.
    `.run()` calls are recorded in `run_calls` for assertion.
    """

    def __init__(self, host: str, user: str | None = None) -> None:
        self.host = host
        self.user = user
        self.get_effect: Exception | None = None
        self.get_contents = b""
        self.run_calls: list[tuple[str, dict]] = []

    def get(self, remote_path: str, destination: str) -> None:
        if self.get_effect is not None:
            raise self.get_effect
        Path(destination).write_bytes(self.get_contents)

    def run(self, command: str, **kwargs) -> None:
        self.run_calls.append((command, kwargs))


def test_offline_mode_recognizes_truthy_values(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "true")
    assert ssh.offline_mode() is True

    monkeypatch.setenv("HOMELAB_OFFLINE", "1")
    assert ssh.offline_mode() is True

    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    assert ssh.offline_mode() is False


def test_offline_diff_marks_remote_paths_as_skipped() -> None:
    status, message = ssh.offline_diff("/etc/example.conf")

    assert status == 3
    assert message == "[?] /etc/example.conf (offline validation; remote diff skipped)"


def test_host_connection_remote_diff_short_circuits_in_offline_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("value\n", encoding="utf-8")

    class DummyConnection:
        def __init__(self, host: str, user: str | None = None) -> None:
            self.host = host
            self.user = user

        def get(self, remote_path: str, destination: str) -> None:
            raise AssertionError("offline mode should skip remote fetch")

    monkeypatch.setattr(ssh, "Connection", DummyConnection)
    monkeypatch.setenv("HOMELAB_OFFLINE", "true")

    connection = ssh.HostConnection("ace")
    status, message = connection.remote_diff(local_file, "/etc/example.conf")

    assert status == 3
    assert message == "[?] /etc/example.conf (offline validation; remote diff skipped)"


def _connection(monkeypatch, dummy: DummyConnection | None = None) -> ssh.HostConnection:
    dummy = dummy or DummyConnection("test-host")
    monkeypatch.setattr(ssh, "Connection", lambda *a, **kw: dummy)
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    return ssh.HostConnection("test-host", user="root", hostname="test-host")


@pytest.mark.parametrize(
    "get_effect,expected_status,expected_message",
    [
        (UnexpectedExit(result=None), 2, "[NEW] /etc/example.conf"),
        (FileNotFoundError(), 2, "[NEW] /etc/example.conf"),
        (PermissionError(), 0, "[?] /etc/example.conf (not diffable: permission denied)"),
        (
            OSError(13, "Permission denied"),
            0,
            "[?] /etc/example.conf (not diffable: permission denied)",
        ),
    ],
)
def test_remote_diff_classifies_fetch_failures(
    monkeypatch, tmp_path: Path, get_effect, expected_status, expected_message
) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("value\n", encoding="utf-8")

    dummy = DummyConnection("test-host")
    dummy.get_effect = get_effect
    connection = _connection(monkeypatch, dummy)

    status, message = connection.remote_diff(local_file, "/etc/example.conf")

    assert status == expected_status
    assert message == expected_message


def test_remote_diff_reraises_unexpected_os_errors(monkeypatch, tmp_path: Path) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("value\n", encoding="utf-8")

    dummy = DummyConnection("test-host")
    dummy.get_effect = OSError(5, "Input/output error")  # not EACCES
    connection = _connection(monkeypatch, dummy)

    with pytest.raises(OSError):
        connection.remote_diff(local_file, "/etc/example.conf")


def test_remote_diff_reports_no_changes_when_content_matches(
    monkeypatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("same\n", encoding="utf-8")

    dummy = DummyConnection("test-host")
    dummy.get_contents = b"same\n"
    connection = _connection(monkeypatch, dummy)

    status, message = connection.remote_diff(local_file, "/etc/example.conf")

    assert status == 0
    assert message == "[=] /etc/example.conf (no changes)"


def test_remote_diff_reports_change_when_content_differs(
    monkeypatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("new\n", encoding="utf-8")

    dummy = DummyConnection("test-host")
    dummy.get_contents = b"old\n"
    connection = _connection(monkeypatch, dummy)

    status, message = connection.remote_diff(local_file, "/etc/example.conf")

    assert status == 1
    assert message == "[~] /etc/example.conf"


def test_remote_diff_cleans_up_temp_file_on_success(monkeypatch, tmp_path: Path) -> None:
    local_file = tmp_path / "local.txt"
    local_file.write_text("same\n", encoding="utf-8")

    captured: dict[str, Path] = {}

    class RecordingConnection(DummyConnection):
        def get(self, remote_path: str, destination: str) -> None:
            captured["path"] = Path(destination)
            super().get(remote_path, destination)

    dummy = RecordingConnection("test-host")
    dummy.get_contents = b"same\n"
    connection = _connection(monkeypatch, dummy)
    connection.remote_diff(local_file, "/etc/example.conf")

    assert not captured["path"].exists()


def test_run_remote_installer_builds_minimal_command(monkeypatch) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.run_remote_installer("/tmp/build", "install.sh")

    assert len(dummy.run_calls) == 1
    command, kwargs = dummy.run_calls[0]
    assert command.rstrip() == "cd /tmp/build && chmod +x install.sh && install.sh"
    assert kwargs == {"pty": False}


def test_run_remote_installer_quotes_args_with_shell_metacharacters(monkeypatch) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    dangerous = "$(rm -rf /); echo `hi` \"quoted\" 'single'"
    connection.run_remote_installer("/tmp/build", "install.sh", dangerous)

    command, _ = dummy.run_calls[0]
    # Round-trip through the shell tokenizer: the dangerous string must survive
    # as one literal argument, not be expanded or split.
    tokens = shlex.split(command)
    assert dangerous in tokens


def test_run_remote_installer_adds_root_check_when_required(monkeypatch) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.run_remote_installer("/tmp/build", "install.sh", require_root=True)

    command, _ = dummy.run_calls[0]
    assert 'if [ "$(id -u)" -ne 0 ]' in command
    assert "deploy requires root SSH user" in command
    # The guard must run between the chmod and the final installer invocation.
    assert "chmod +x install.sh && if" in command
    assert command.rstrip().endswith("fi && install.sh")


def test_run_remote_installer_prepends_interpreter(monkeypatch) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.run_remote_installer("/tmp/build", "install.sh", interpreter="bash")

    command, _ = dummy.run_calls[0]
    assert "bash install.sh" in command


def test_run_remote_installer_exports_env_with_quoted_values(monkeypatch) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.run_remote_installer(
        "/tmp/build", "install.sh", env={"TOKEN": "a$b`c"}
    )

    command, _ = dummy.run_calls[0]
    assert "env TOKEN=" in command
    tokens = shlex.split(command)
    assert "TOKEN=a$b`c" in tokens


def test_build_files_returns_sorted_relative_file_paths(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    (build_dir / "nested").mkdir(parents=True)
    (build_dir / "b.txt").write_text("b\n", encoding="utf-8")
    (build_dir / "nested" / "a.txt").write_text("a\n", encoding="utf-8")

    assert ssh.build_files(build_dir) == ["b.txt", "nested/a.txt"]
