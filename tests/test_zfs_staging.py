"""The diff / upload / dry-run report helpers behind `zfs_automation.deploy_host`.

`deploy_host` used to carry two near-identical arms — one for hosts with staged
secrets and one for hosts without — each rebuilding the diff pairs, the upload set,
and the installer call. Anything fixed in one arm had to be remembered in the other.
These helpers are the shared extraction; the tests pin the two properties that
differ between the arms and would otherwise drift silently:

  - a private key is uploaded as its own tmpfs path, never from the build dir
    (which is persistent and mode 0644 in the repo working tree), and
  - a dry run neither renders nor references a secret, so it must not diff or
    upload one.
"""

from __future__ import annotations

from pathlib import Path

from homelab.modules.zfs_automation import staging
from homelab.modules.zfs_automation.types import (
    REMOTE_ROOT,
    FileSpec,
    HostArtifacts,
    SecretFileSpec,
)

CONFIG = FileSpec("sanoid.conf", "/etc/sanoid/sanoid.conf", "644")
UNIT = FileSpec("homelab-zfs-snapshots.service", "/etc/systemd/system/x.service", "644")
KEY = SecretFileSpec("zfs-push.key", "/root/.ssh/zfs-push", "zfs-push-key", "600")


def _artifacts(build_dir: Path, *, with_secret: bool = True) -> HostArtifacts:
    return HostArtifacts(
        build_dir=build_dir,
        file_specs=(CONFIG, UNIT),
        secret_file_specs=(KEY,) if with_secret else (),
    )


class TestDiffPairs:
    def test_build_files_are_compared_against_their_remote_paths(self, tmp_path: Path) -> None:
        pairs = staging.diff_pairs_for(_artifacts(tmp_path, with_secret=False), {})

        assert pairs == [
            (tmp_path / "sanoid.conf", "/etc/sanoid/sanoid.conf"),
            (tmp_path / "homelab-zfs-snapshots.service", "/etc/systemd/system/x.service"),
        ]

    def test_staged_secrets_are_compared_from_their_tmpfs_path(self, tmp_path: Path) -> None:
        staged = tmp_path / "shm" / "zfs-push.key"
        pairs = staging.diff_pairs_for(_artifacts(tmp_path), {"zfs-push.key": staged})

        assert pairs[-1] == (staged, "/root/.ssh/zfs-push")
        assert all(pair[0] != tmp_path / "zfs-push.key" for pair in pairs)

    def test_unstaged_secret_is_not_diffed(self, tmp_path: Path) -> None:
        """The dry-run case: the key was never rendered, so there is no local side."""
        pairs = staging.diff_pairs_for(_artifacts(tmp_path), {})

        assert [remote for _, remote in pairs] == [
            "/etc/sanoid/sanoid.conf",
            "/etc/systemd/system/x.service",
        ]


class TestUploadPaths:
    def test_build_dir_and_scripts_are_always_uploaded(self, tmp_path: Path) -> None:
        paths = staging.upload_paths_for(
            tmp_path / "zfs-automation", "ace", _artifacts(tmp_path, with_secret=False), {}
        )

        assert paths == [
            (tmp_path, f"{REMOTE_ROOT}/build/ace"),
            (tmp_path / "zfs-automation" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ]

    def test_secret_is_uploaded_individually_from_tmpfs(self, tmp_path: Path) -> None:
        staged = tmp_path / "shm" / "zfs-push.key"

        paths = staging.upload_paths_for(
            tmp_path / "zfs-automation", "ace", _artifacts(tmp_path), {"zfs-push.key": staged}
        )

        assert paths[-1] == (staged, f"{REMOTE_ROOT}/build/ace/zfs-push.key")

    def test_unstaged_secret_is_not_uploaded(self, tmp_path: Path) -> None:
        paths = staging.upload_paths_for(
            tmp_path / "zfs-automation", "ace", _artifacts(tmp_path), {}
        )

        assert len(paths) == 2


class TestDryRunReport:
    class FakeJob:
        def __init__(self, name: str, paused: bool) -> None:
            self.name = name
            self.paused = paused

    def test_module_wide_pause_is_reported_instead_of_per_job(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        """`paused: true` freezes every timer, so listing individual jobs would mislead."""
        monkeypatch.setattr(staging, "feature_paused", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            staging,
            "normalize_replication_config",
            lambda registry, host: [self.FakeJob("cluster", True)],
        )

        staging.report_dry_run(object(), "ace", _artifacts(tmp_path, with_secret=False))

        output = capsys.readouterr().out
        assert "Would pause zfs-automation on ace" in output
        assert "replication job" not in output

    def test_per_job_pause_names_only_the_paused_jobs(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        monkeypatch.setattr(staging, "feature_paused", lambda *args, **kwargs: False)
        monkeypatch.setattr(
            staging,
            "normalize_replication_config",
            lambda registry, host: [
                self.FakeJob("cluster", True),
                self.FakeJob("offsite", False),
            ],
        )

        staging.report_dry_run(object(), "ace", _artifacts(tmp_path, with_secret=False))

        output = capsys.readouterr().out
        assert "replication job 'cluster'" in output
        assert "offsite" not in output

    def test_secrets_are_listed_as_deploy_time_only(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        monkeypatch.setattr(staging, "feature_paused", lambda *args, **kwargs: False)
        monkeypatch.setattr(staging, "normalize_replication_config", lambda registry, host: [])
        (tmp_path / "sanoid.conf").write_text("x\n", encoding="utf-8")

        staging.report_dry_run(object(), "ace", _artifacts(tmp_path))

        output = capsys.readouterr().out
        assert "Secret files staged only during real deploy:" in output
        assert "zfs-push.key" in output
