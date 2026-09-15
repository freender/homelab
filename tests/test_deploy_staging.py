"""Tests for `stage_and_run_remote_installer` and its Python-installer plumbing.

`stage_and_run_remote_installer` is the single door every module goes through to
reach a host, and until now nothing asserted its body — `test_dry_run_all_modules.py`
returns before it is called, and every module test patches it out wholesale. That was
tolerable while it did three unconditional things; it stopped being tolerable once it
started *deciding* something (freender/homelab-ops#34).

The decision is worth pinning precisely because it is implicit: a module declares
`scripts/install.py` and never asks for `lib/py/` or `PYTHONPATH`. If either half of
that inference broke, the symptom would be a `ModuleNotFoundError` raised as root on
the target, mid-deploy, with the bundle already staged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homelab import deploy


class RecordingConnection:
    """Stands in for `HostConnection`, recording the staging calls in order."""

    def __init__(self) -> None:
        self.prepared: list[tuple[str, tuple[str, ...]]] = []
        self.uploaded: list[list[tuple[Path, str]]] = []
        self.python_libs: list[tuple[Path, str]] = []
        self.installer_calls: list[dict[str, Any]] = []

    def prepare_remote_dir(self, remote_root: str, *subdirs: str) -> None:
        self.prepared.append((remote_root, subdirs))

    def upload_paths(self, paths: list[tuple[Path, str]]) -> None:
        self.uploaded.append(paths)

    def upload_python_lib(self, root: Path, remote_root: str) -> None:
        self.python_libs.append((root, remote_root))

    def run_remote_installer(self, remote_dir: str, installer: str, *args: str, **kwargs) -> None:
        self.installer_calls.append(
            {"remote_dir": remote_dir, "installer": installer, "args": args, **kwargs}
        )


def _stage(connection: RecordingConnection, installer: str, **kwargs) -> None:
    deploy.stage_and_run_remote_installer(
        Path("/repo"),
        connection,  # type: ignore[arg-type]
        "/tmp/homelab-demo",
        [(Path("/repo/demo/scripts"), "/tmp/homelab-demo/scripts")],
        installer,
        "beta",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# is_python_installer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "installer,expected",
    [
        ("scripts/install.py", True),
        ("scripts/install.sh", False),
        # A name that merely *contains* the suffix is a bash installer. Substring
        # matching here would upload the Python library for the wrong module.
        ("scripts/install.py.sh", False),
        ("scripts/sync-answers.py", True),
    ],
)
def test_is_python_installer_keys_on_the_suffix_only(installer: str, expected: bool) -> None:
    assert deploy.is_python_installer(installer) is expected


# ---------------------------------------------------------------------------
# python_lib_env
# ---------------------------------------------------------------------------


def test_python_lib_env_sets_pythonpath_when_there_is_no_env() -> None:
    assert deploy.python_lib_env("/tmp/homelab-demo", None) == {
        "PYTHONPATH": "/tmp/homelab-demo/lib/py"
    }


def test_python_lib_env_keeps_the_callers_other_variables() -> None:
    env = deploy.python_lib_env("/tmp/homelab-demo", {"FORCE_UPDATE": "true"})

    assert env == {
        "FORCE_UPDATE": "true",
        "PYTHONPATH": "/tmp/homelab-demo/lib/py",
    }


def test_python_lib_env_prepends_rather_than_replacing_an_existing_pythonpath() -> None:
    """Order is the assertion: the staged library has to win against a same-named
    module already on the path, and the caller's entry must survive."""
    env = deploy.python_lib_env("/tmp/homelab-demo", {"PYTHONPATH": "/opt/vendor"})

    assert env["PYTHONPATH"] == "/tmp/homelab-demo/lib/py:/opt/vendor"


def test_python_lib_env_does_not_mutate_the_callers_dict() -> None:
    original = {"FORCE_UPDATE": "false"}

    deploy.python_lib_env("/tmp/homelab-demo", original)

    assert original == {"FORCE_UPDATE": "false"}


# ---------------------------------------------------------------------------
# stage_and_run_remote_installer
# ---------------------------------------------------------------------------


def test_staging_a_bash_installer_uploads_no_library_at_all() -> None:
    """The three `pve-*-patch` modules are the only bash installers left, and they
    source nothing — so their bundle is now their own files and nothing else. They
    must not gain a `PYTHONPATH` or a `lib/py/` upload either."""
    connection = RecordingConnection()

    _stage(connection, "scripts/install.sh", env={"FORCE_UPDATE": "false"})

    assert connection.python_libs == []
    assert connection.installer_calls[0]["env"] == {"FORCE_UPDATE": "false"}


def test_staging_a_bash_installer_with_no_env_still_passes_none() -> None:
    connection = RecordingConnection()

    _stage(connection, "scripts/install.sh")

    assert connection.installer_calls[0]["env"] is None


def test_staging_a_python_installer_uploads_the_shared_library() -> None:
    connection = RecordingConnection()

    _stage(connection, "scripts/install.py", interpreter="python3")

    assert connection.python_libs == [(Path("/repo"), "/tmp/homelab-demo")]


def test_staging_a_python_installer_sets_pythonpath_alongside_force_update() -> None:
    connection = RecordingConnection()

    _stage(connection, "scripts/install.py", env=deploy.force_env(True), interpreter="python3")

    assert connection.installer_calls[0]["env"] == {
        "FORCE_UPDATE": "true",
        "PYTHONPATH": "/tmp/homelab-demo/lib/py",
    }


def test_staging_a_python_installer_sets_pythonpath_even_without_a_caller_env() -> None:
    connection = RecordingConnection()

    _stage(connection, "scripts/install.py", interpreter="python3")

    assert connection.installer_calls[0]["env"] == {"PYTHONPATH": "/tmp/homelab-demo/lib/py"}


def test_staging_passes_the_interpreter_and_installer_through_untouched() -> None:
    connection = RecordingConnection()

    _stage(connection, "scripts/install.py", interpreter="python3", require_root=True)

    call = connection.installer_calls[0]
    assert call["remote_dir"] == "/tmp/homelab-demo"
    assert call["installer"] == "scripts/install.py"
    assert call["args"] == ("beta",)
    assert call["interpreter"] == "python3"
    assert call["require_root"] is True


def test_staging_prepares_the_remote_dir_before_uploading_anything() -> None:
    connection = RecordingConnection()

    _stage(connection, "scripts/install.py", interpreter="python3")

    # prepare_remote_dir does `rm -rf` on the root; an upload ordered before it
    # would be silently deleted.
    assert connection.prepared == [("/tmp/homelab-demo", ("build", "lib"))]
    assert connection.uploaded == [[(Path("/repo/demo/scripts"), "/tmp/homelab-demo/scripts")]]
