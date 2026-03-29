from __future__ import annotations

from pathlib import Path

from homelab import ssh


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
        def __init__(self, host: str) -> None:
            self.host = host

        def get(self, remote_path: str, destination: str) -> None:
            raise AssertionError("offline mode should skip remote fetch")

    monkeypatch.setattr(ssh, "Connection", DummyConnection)
    monkeypatch.setenv("HOMELAB_OFFLINE", "true")

    connection = ssh.HostConnection("ace")
    status, message = connection.remote_diff(local_file, "/etc/example.conf")

    assert status == 3
    assert message == "[?] /etc/example.conf (offline validation; remote diff skipped)"


def test_build_files_returns_sorted_relative_file_paths(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    (build_dir / "nested").mkdir(parents=True)
    (build_dir / "b.txt").write_text("b\n", encoding="utf-8")
    (build_dir / "nested" / "a.txt").write_text("a\n", encoding="utf-8")

    assert ssh.build_files(build_dir) == ["b.txt", "nested/a.txt"]
