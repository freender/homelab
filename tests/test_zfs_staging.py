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

import shutil
from pathlib import Path

import pytest

from homelab import op_secrets
from homelab.modules.zfs_automation import staging
from homelab.modules.zfs_automation.types import (
    REMOTE_ROOT,
    FileSpec,
    HostArtifacts,
    KnownHostRefresh,
    ReplicationJob,
    ReplicationPlan,
    SecretFileSpec,
    SnapshotPlan,
    SourcePrivateKey,
    ZfsPusher,
    ZfsPushTargetAccess,
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


# ---------------------------------------------------------------------------
# build_host_artifacts: the file map and the env, per host shape.
#
# This function was at 100% coverage purely because test_dry_run_all_modules.py
# executes it and asserts exit_code == 0 — the known hole in the CRAP metric.
# Nothing checked *what* it built. These tests drive the flag matrix through a
# stub registry and assert the two outputs install.sh actually consumes:
# file-map.conf (which files land where, in order) and env (what gets enabled,
# and what gets frozen).
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
TEST_HOST = "zfs-staging-test"

PLAN = SnapshotPlan(
    dataset="tank/data",
    hourly="36",
    daily="14",
    weekly="8",
    monthly="6",
    yearly="0",
)
JOB = ReplicationJob(
    name="tank-to-remote",
    schedule="*-*-* 02:00:00",
    plans=(ReplicationPlan(target="remote:tank/data", source="tank/data"),),
    syncoid_options=("--no-sync-snap",),
    delete_target_snapshots=False,
)


@pytest.fixture
def zfs_build(monkeypatch: pytest.MonkeyPatch):
    """Build artifacts for a synthetic host, then remove its build dir.

    Real templates from the repo are used (they are the thing being rendered);
    only the inventory and the normalize/access layer are stubbed, so each test
    states exactly the host shape it is about.
    """
    build_dir = ROOT / "zfs-automation" / "build" / TEST_HOST

    def build(**shape):
        values = {
            "config.homelab_state_dir": "/var/lib/homelab",
            "zfs-automation.snapshot_schedule": "*-*-* 03:00:00",
            "zfs-automation.manage_snapshots": shape.get("manage_snapshots", True),
            "zfs-automation.manage_scrub": shape.get("manage_scrub", True),
            "zfs-automation.replication_recovery.start_failed": shape.get(
                "start_failed", False
            ),
        }

        class Registry:
            def get(self, _host: str, key: str, default: object = None) -> object:
                return values.get(key, default)

        monkeypatch.setattr(staging, "default_registry", lambda _root: Registry())
        monkeypatch.setattr(
            staging, "feature_paused", lambda *_a: shape.get("paused", False)
        )
        monkeypatch.setattr(staging, "resolve_pools", lambda *_a: shape.get("pools", ["tank"]))
        monkeypatch.setattr(
            staging, "normalize_snapshot_plans", lambda *_a: shape.get("snapshot_plans", [PLAN])
        )
        monkeypatch.setattr(
            staging,
            "normalize_replication_config",
            lambda *_a: shape.get("replication_jobs", []),
        )
        monkeypatch.setattr(
            staging,
            "normalize_known_host_refresh",
            lambda *_a: shape.get("known_host_refresh", []),
        )
        monkeypatch.setattr(
            staging,
            "normalize_push_target_access",
            lambda *_a: shape.get("push_target_access"),
        )
        monkeypatch.setattr(
            staging,
            "normalize_source_private_keys",
            lambda *_a: shape.get("source_private_keys", []),
        )
        return staging.build_host_artifacts(ROOT, TEST_HOST)

    try:
        yield build
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _env(artifacts) -> dict[str, str]:
    """Read the built env the way a consumer does, not by string-splitting it."""
    return op_secrets.parse_env_file(artifacts.build_dir / "env")


class TestBuildHostArtifactsFileMap:
    def test_a_plain_host_gets_only_the_base_specs(self, zfs_build) -> None:
        artifacts = zfs_build()

        assert artifacts.file_specs == staging.BASE_FILE_SPECS
        assert artifacts.secret_file_specs == ()

    def test_file_map_lists_every_spec_in_order(self, zfs_build) -> None:
        """install.sh applies file-map.conf top to bottom, so order is contract."""
        artifacts = zfs_build(replication_jobs=[JOB])
        lines = (artifacts.build_dir / "file-map.conf").read_text(encoding="utf-8").splitlines()

        assert lines == [
            f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in artifacts.file_specs
        ]

    def test_known_host_refresh_adds_its_script_only_when_declared(self, zfs_build) -> None:
        without = [spec.build_name for spec in zfs_build().file_specs]
        assert "homelab-zfs-refresh-known-hosts.sh" not in without

        artifacts = zfs_build(known_host_refresh=[KnownHostRefresh(host="10.0.0.20")])
        names = [spec.build_name for spec in artifacts.file_specs]

        assert "homelab-zfs-refresh-known-hosts.sh" in names
        assert (artifacts.build_dir / "homelab-zfs-refresh-known-hosts.sh").is_file()

    def test_each_replication_job_contributes_a_unit_timer_and_script(
        self, zfs_build
    ) -> None:
        artifacts = zfs_build(replication_jobs=[JOB])
        names = [spec.build_name for spec in artifacts.file_specs]

        assert names[-3:] == [
            "homelab-zfs-replication-tank-to-remote.service",
            "homelab-zfs-replication-tank-to-remote.timer",
            "homelab-zfs-replication-tank-to-remote.sh",
        ]
        modes = {spec.build_name: spec.mode for spec in artifacts.file_specs}
        assert modes["homelab-zfs-replication-tank-to-remote.sh"] == "755"

    def test_replication_timer_carries_the_job_schedule(self, zfs_build) -> None:
        artifacts = zfs_build(replication_jobs=[JOB])
        timer = (
            artifacts.build_dir / "homelab-zfs-replication-tank-to-remote.timer"
        ).read_text(encoding="utf-8")

        assert "*-*-* 02:00:00" in timer

    def test_push_target_writes_the_dataset_allow_list_and_authorized_keys(
        self, zfs_build
    ) -> None:
        access = ZfsPushTargetAccess(
            enabled=True,
            user="zfs-push",
            datasets=("backup/replica",),
            pushers=(
                ZfsPusher(
                    name="ace",
                    from_address="10.0.0.11",
                    public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest",
                ),
            ),
        )

        artifacts = zfs_build(push_target_access=access)
        names = [spec.build_name for spec in artifacts.file_specs]

        assert "zfs-push-authorized-keys" in names
        assert (artifacts.build_dir / "zfs-push-datasets.conf").read_text(
            encoding="utf-8"
        ) == "backup/replica\n"
        keys = (artifacts.build_dir / "zfs-push-authorized-keys").read_text(encoding="utf-8")
        assert 'from="10.0.0.11"' in keys
        assert "homelab-zfs-receive-only backup/replica" in keys

    def test_authorized_keys_is_not_group_or_world_readable(self, zfs_build) -> None:
        """It gates who may push into the replica; 0644 would publish the allow-list."""
        access = ZfsPushTargetAccess(
            enabled=True, user="zfs-push", datasets=("backup/replica",), pushers=()
        )
        artifacts = zfs_build(push_target_access=access)
        modes = {spec.build_name: spec.mode for spec in artifacts.file_specs}

        assert modes["zfs-push-authorized-keys"] == "600"

    def test_source_private_keys_become_secret_specs_not_build_files(
        self, zfs_build
    ) -> None:
        """A private key must never be written into the persistent build dir."""
        artifacts = zfs_build(
            source_private_keys=[
                SourcePrivateKey(secret="zfs-replication-key", path="/root/.ssh/id_zfs")
            ]
        )

        assert [spec.remote_path for spec in artifacts.secret_file_specs] == [
            "/root/.ssh/id_zfs"
        ]
        assert not (artifacts.build_dir / "source-private-key-0").exists()
        # Still listed in the file map, so install.sh knows where it goes.
        file_map = (artifacts.build_dir / "file-map.conf").read_text(encoding="utf-8")
        assert "source-private-key-0|/root/.ssh/id_zfs" in file_map


class TestBuildHostArtifactsEnv:
    def test_defaults_enable_snapshots_and_scrub_but_not_replication(
        self, zfs_build
    ) -> None:
        env = _env(zfs_build())

        assert env["ENABLE_ZFS_SNAPSHOTS"] == "true"
        assert env["ENABLE_ZFS_SCRUB"] == "true"
        assert env["ENABLE_ZFS_REPLICATION"] == "false"
        assert env["PAUSED"] == "false"

    def test_manage_snapshots_false_disables_them_despite_declared_plans(
        self, zfs_build
    ) -> None:
        env = _env(zfs_build(manage_snapshots=False))

        assert env["ENABLE_ZFS_SNAPSHOTS"] == "false"

    def test_no_declared_plans_disables_snapshots_even_when_managed(
        self, zfs_build
    ) -> None:
        env = _env(zfs_build(snapshot_plans=[]))

        assert env["ENABLE_ZFS_SNAPSHOTS"] == "false"

    def test_manage_scrub_false_or_no_pools_disables_scrub(self, zfs_build) -> None:
        assert _env(zfs_build(manage_scrub=False))["ENABLE_ZFS_SCRUB"] == "false"
        assert _env(zfs_build(pools=[]))["ENABLE_ZFS_SCRUB"] == "false"

    def test_module_wide_pause_is_a_single_flag_that_overrides_manage_flags(
        self, zfs_build
    ) -> None:
        """`paused` freezes the timers; it does not retract what is deployed."""
        env = _env(zfs_build(paused=True, replication_jobs=[JOB]))

        assert env["PAUSED"] == "true"
        assert env["ENABLE_ZFS_SNAPSHOTS"] == "true"  # still installed, just stopped
        assert env["ENABLE_ZFS_REPLICATION"] == "true"

    def test_per_job_pause_names_only_the_paused_timer(self, zfs_build) -> None:
        paused_job = ReplicationJob(
            name="offsite",
            schedule="*-*-* 04:00:00",
            plans=(ReplicationPlan(target="remote:tank", source="tank"),),
            syncoid_options=(),
            delete_target_snapshots=False,
            paused=True,
        )

        env = _env(zfs_build(replication_jobs=[JOB, paused_job]))

        assert env["PAUSED_REPLICATION_TIMERS"] == "homelab-zfs-replication-offsite.timer"
        assert env["PAUSED"] == "false"  # not a module-wide freeze

    def test_no_paused_jobs_leaves_the_timer_list_empty(self, zfs_build) -> None:
        env = _env(zfs_build(replication_jobs=[JOB]))

        assert env.get("PAUSED_REPLICATION_TIMERS", "") == ""

    def test_push_target_user_defaults_when_the_host_receives_no_push(
        self, zfs_build
    ) -> None:
        env = _env(zfs_build())

        assert env["ENABLE_ZFS_PUSH_TARGET"] == "false"
        assert env["ZFS_PUSH_TARGET_USER"] == "zfs-push"

    def test_push_target_user_comes_from_the_declared_access(self, zfs_build) -> None:
        access = ZfsPushTargetAccess(
            enabled=True, user="replica-recv", datasets=("backup/replica",), pushers=()
        )

        env = _env(zfs_build(push_target_access=access))

        assert env["ENABLE_ZFS_PUSH_TARGET"] == "true"
        assert env["ZFS_PUSH_TARGET_USER"] == "replica-recv"

    def test_recovery_start_failed_is_passed_through(self, zfs_build) -> None:
        assert _env(zfs_build())["ZFS_REPLICATION_RECOVERY_START_FAILED"] == "false"
        assert (
            _env(zfs_build(start_failed=True))["ZFS_REPLICATION_RECOVERY_START_FAILED"]
            == "true"
        )

    def test_retired_pull_source_inputs_stay_false_for_cleanup(self, zfs_build) -> None:
        """install.sh reads these to remove artifacts from old releases."""
        env = _env(zfs_build())

        assert env["ENABLE_ZFS_PULL_SOURCE"] == "false"
        assert env["ZFS_PULL_SOURCE_USER"] == "zfs-pull"
