"""ssh-config's remote diff — the one part of the module that talks to a host.

This is what `./deploy --dry-run ssh-config <host>` prints, and it is the only
preview an operator gets before overwriting their own ~/.ssh/config. A diff that
misreports "no changes" would make a real rewrite look like a no-op, so each of
the four verdicts is pinned here.

The module reaches for fabric's Transfer directly rather than going through
HostConnection.remote_diff, because the path is relative to the SSH user's home
and only the remote side can expand it. That means ssh.py's tests do not cover
it: fabric.transfer.Transfer is stubbed instead.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from invoke.exceptions import UnexpectedExit

from homelab.modules import ssh_config


class DummyTransfer:
    """Stand-in for fabric.transfer.Transfer.

    `effect` is raised by get(); otherwise `contents` lands at the destination.
    """

    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.effect: Exception | None = None
        self.contents = b""
        self.get_calls: list[tuple[str, str]] = []

    def get(self, remote_path: str, local_path: str) -> None:
        self.get_calls.append((remote_path, local_path))
        if self.effect is not None:
            raise self.effect
        Path(local_path).write_bytes(self.contents)


class DummyConnection:
    """Minimal HostConnection stand-in: only `.connection` is ever read."""

    connection = object()


@pytest.fixture
def transfer(monkeypatch: pytest.MonkeyPatch) -> DummyTransfer:
    dummy = DummyTransfer(DummyConnection.connection)
    monkeypatch.setattr(ssh_config, "Transfer", lambda _connection: dummy)
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    return dummy


def _local(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return path


def test_offline_mode_skips_the_fetch_entirely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    def boom(_connection):
        raise AssertionError("offline validation must not open a transfer")

    monkeypatch.setattr(ssh_config, "Transfer", boom)

    status, message = ssh_config.dry_run_remote_diff(
        DummyConnection(), _local(tmp_path, "Host ace\n")
    )

    assert status == 3
    assert message == "[?] $HOME/.ssh/config (offline validation; remote diff skipped)"


def test_identical_content_reports_no_changes(
    transfer: DummyTransfer, tmp_path: Path
) -> None:
    transfer.contents = b"Host ace\n  User root\n"

    status, message = ssh_config.dry_run_remote_diff(
        DummyConnection(), _local(tmp_path, "Host ace\n  User root\n")
    )

    assert status == 0
    assert message == "[=] $HOME/.ssh/config (no changes)"


def test_differing_content_reports_a_modification(
    transfer: DummyTransfer, tmp_path: Path
) -> None:
    transfer.contents = b"Host ace\n  User freender\n"

    status, message = ssh_config.dry_run_remote_diff(
        DummyConnection(), _local(tmp_path, "Host ace\n  User root\n")
    )

    assert status == 1
    assert message == "[~] $HOME/.ssh/config"


def test_trailing_byte_difference_is_not_reported_as_no_changes(
    transfer: DummyTransfer, tmp_path: Path
) -> None:
    """Byte comparison, not a normalized one: a whitespace-only edit still deploys."""
    transfer.contents = b"Host ace\n  User root\n\n"

    status, _message = ssh_config.dry_run_remote_diff(
        DummyConnection(), _local(tmp_path, "Host ace\n  User root\n")
    )

    assert status == 1


@pytest.mark.parametrize(
    "effect", [FileNotFoundError("no such file"), UnexpectedExit(result=None)]
)
def test_absent_remote_file_reports_new(
    transfer: DummyTransfer, tmp_path: Path, effect: Exception
) -> None:
    transfer.effect = effect

    status, message = ssh_config.dry_run_remote_diff(
        DummyConnection(), _local(tmp_path, "Host ace\n")
    )

    assert status == 2
    assert message == "[NEW] $HOME/.ssh/config"


def test_remote_path_is_relative_so_the_remote_home_expands_it(
    transfer: DummyTransfer, tmp_path: Path
) -> None:
    """An absolute /home/<user>/.ssh/config would break for root and for exo."""
    ssh_config.dry_run_remote_diff(DummyConnection(), _local(tmp_path, "Host ace\n"))

    assert transfer.get_calls[0][0] == ".ssh/config"


def test_temp_directory_is_removed_after_a_successful_diff(
    transfer: DummyTransfer, tmp_path: Path
) -> None:
    ssh_config.dry_run_remote_diff(DummyConnection(), _local(tmp_path, "Host ace\n"))

    fetched = Path(transfer.get_calls[0][1])
    assert not fetched.exists()
    assert not fetched.parent.exists()


def test_temp_directory_is_removed_even_when_the_fetch_fails(
    transfer: DummyTransfer, tmp_path: Path
) -> None:
    transfer.effect = FileNotFoundError("no such file")

    ssh_config.dry_run_remote_diff(DummyConnection(), _local(tmp_path, "Host ace\n"))

    assert not Path(transfer.get_calls[0][1]).parent.exists()


def test_temp_directory_is_removed_when_the_comparison_raises(
    transfer: DummyTransfer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cleanup is a `finally`, so an unexpected error cannot leak the temp dir."""
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(ssh_config.tempfile, "mkdtemp", recording_mkdtemp)
    local_file = tmp_path / "config"  # never written -> read_bytes raises

    with pytest.raises(OSError):
        ssh_config.dry_run_remote_diff(DummyConnection(), local_file)

    assert created and not Path(created[0]).exists()
