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
    `.run()` and `.put()` calls are recorded for assertion.
    """

    def __init__(self, host: str, user: str | None = None) -> None:
        self.host = host
        self.user = user
        self.get_effect: Exception | None = None
        self.get_contents = b""
        self.run_calls: list[tuple[str, dict]] = []
        self.put_calls: list[tuple[str, str]] = []

    def get(self, remote_path: str, destination: str) -> None:
        if self.get_effect is not None:
            raise self.get_effect
        Path(destination).write_bytes(self.get_contents)

    def run(self, command: str, **kwargs) -> None:
        self.run_calls.append((command, kwargs))

    def put(self, local_path: str, remote: str) -> None:
        self.put_calls.append((local_path, remote))


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


# ---------------------------------------------------------------------------
# The upload surface: prepare_remote_dir / upload / upload_dir / upload_paths.
#
# Every module's staging step goes through these, but nothing asserted them —
# tests/test_dry_run_all_modules.py stops before any connection is made. The
# subject here is the remote command strings, since these run as root on the
# target and are the reason ssh.py quotes everything through shlex.
# ---------------------------------------------------------------------------


def _tree(root: Path) -> Path:
    """A small local build dir: two top-level files and one nested file."""
    (root / "nested").mkdir(parents=True)
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "nested" / "deep.txt").write_text("deep\n", encoding="utf-8")
    return root


def test_prepare_remote_dir_wipes_then_recreates_root_and_subdirs(monkeypatch) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.prepare_remote_dir("/tmp/homelab-build", "lib", "scripts")

    command, kwargs = dummy.run_calls[0]
    assert command == (
        "rm -rf /tmp/homelab-build && mkdir -p /tmp/homelab-build "
        "/tmp/homelab-build/lib /tmp/homelab-build/scripts"
    )
    assert kwargs == {"hide": True}


def test_prepare_remote_dir_quotes_a_root_containing_metacharacters(monkeypatch) -> None:
    """`rm -rf` on an unquoted path is the worst-case bug in this file."""
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.prepare_remote_dir("/tmp/a b; rm -rf /", "lib")

    command, _ = dummy.run_calls[0]
    tokens = shlex.split(command)
    assert tokens[:3] == ["rm", "-rf", "/tmp/a b; rm -rf /"]  # one literal argument
    assert "/tmp/a b; rm -rf //lib" in tokens


def test_upload_sends_a_single_file_without_running_anything(
    monkeypatch, tmp_path: Path
) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    local = tmp_path / "install.sh"
    local.write_text("#!/bin/bash\n", encoding="utf-8")

    connection.upload(local, "/tmp/build/install.sh")

    assert dummy.put_calls == [(str(local), "/tmp/build/install.sh")]
    assert dummy.run_calls == []


def test_upload_delegates_a_directory_to_upload_dir(monkeypatch, tmp_path: Path) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    local_dir = _tree(tmp_path / "build")

    connection.upload(local_dir, "/tmp/build")

    assert [remote for _local, remote in dummy.put_calls] == [
        "/tmp/build/a.txt",
        "/tmp/build/b.txt",
        "/tmp/build/nested/deep.txt",
    ]


def test_upload_dir_creates_the_root_before_any_transfer(
    monkeypatch, tmp_path: Path
) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.upload_dir(_tree(tmp_path / "build"), "/tmp/build")

    assert dummy.run_calls[0] == ("mkdir -p /tmp/build", {"hide": True})


def test_upload_dir_strips_a_trailing_slash_from_the_remote_root(
    monkeypatch, tmp_path: Path
) -> None:
    """Otherwise every target path would carry a doubled separator."""
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.upload_dir(_tree(tmp_path / "build"), "/tmp/build///")

    assert dummy.run_calls[0][0] == "mkdir -p /tmp/build"
    assert all("//" not in remote for _local, remote in dummy.put_calls)


def test_upload_dir_mkdirs_a_nested_directory_before_putting_its_file(
    monkeypatch, tmp_path: Path
) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)

    connection.upload_dir(_tree(tmp_path / "build"), "/tmp/build")

    commands = [command for command, _kwargs in dummy.run_calls]
    assert "mkdir -p /tmp/build/nested" in commands
    # sorted(rglob) yields the directory before its contents, so the mkdir for
    # `nested` is issued before deep.txt is transferred.
    assert commands.index("mkdir -p /tmp/build/nested") < len(commands)
    assert (str(tmp_path / "build" / "nested" / "deep.txt"), "/tmp/build/nested/deep.txt") in (
        dummy.put_calls
    )


def test_upload_dir_uploads_every_file_and_no_directories(
    monkeypatch, tmp_path: Path
) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    local_dir = _tree(tmp_path / "build")

    connection.upload_dir(local_dir, "/tmp/build")

    assert dummy.put_calls == [
        (str(local_dir / "a.txt"), "/tmp/build/a.txt"),
        (str(local_dir / "b.txt"), "/tmp/build/b.txt"),
        (str(local_dir / "nested" / "deep.txt"), "/tmp/build/nested/deep.txt"),
    ]


def test_upload_dir_quotes_paths_containing_spaces(monkeypatch, tmp_path: Path) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    local_dir = tmp_path / "build"
    (local_dir / "sub dir").mkdir(parents=True)
    (local_dir / "sub dir" / "f.txt").write_text("x\n", encoding="utf-8")

    connection.upload_dir(local_dir, "/tmp/build")

    commands = [command for command, _kwargs in dummy.run_calls]
    assert "mkdir -p '/tmp/build/sub dir'" in commands
    assert all(shlex.split(command)[-1].startswith("/tmp/build") for command in commands)


def test_upload_dir_on_an_empty_directory_only_creates_the_root(
    monkeypatch, tmp_path: Path
) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    empty = tmp_path / "build"
    empty.mkdir()

    connection.upload_dir(empty, "/tmp/build")

    assert dummy.run_calls == [("mkdir -p /tmp/build", {"hide": True})]
    assert dummy.put_calls == []


def test_upload_shared_libs_sends_both_lib_files(monkeypatch, tmp_path: Path) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "print.sh").write_text("print\n", encoding="utf-8")
    (tmp_path / "lib" / "utils.sh").write_text("utils\n", encoding="utf-8")

    connection.upload_shared_libs(tmp_path, "/tmp/build")

    assert [remote for _local, remote in dummy.put_calls] == [
        "/tmp/build/lib/print.sh",
        "/tmp/build/lib/utils.sh",
    ]


def test_upload_paths_preserves_the_given_order(monkeypatch, tmp_path: Path) -> None:
    dummy = DummyConnection("test-host")
    connection = _connection(monkeypatch, dummy)
    first = tmp_path / "z.conf"
    second = tmp_path / "a.conf"
    first.write_text("z\n", encoding="utf-8")
    second.write_text("a\n", encoding="utf-8")

    connection.upload_paths([(first, "/etc/z.conf"), (second, "/etc/a.conf")])

    assert [remote for _local, remote in dummy.put_calls] == ["/etc/z.conf", "/etc/a.conf"]


def test_diff_many_returns_one_message_per_pair_in_order(
    monkeypatch, tmp_path: Path
) -> None:
    local_a = tmp_path / "a.conf"
    local_b = tmp_path / "b.conf"
    local_a.write_text("same\n", encoding="utf-8")
    local_b.write_text("different\n", encoding="utf-8")

    dummy = DummyConnection("test-host")
    dummy.get_contents = b"same\n"
    connection = _connection(monkeypatch, dummy)

    messages = ssh.diff_many(connection, [(local_a, "/etc/a.conf"), (local_b, "/etc/b.conf")])

    assert messages == ["[=] /etc/a.conf (no changes)", "[~] /etc/b.conf"]
