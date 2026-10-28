"""The diff / upload / dry-run report helpers behind `zfs_automation.deploy_host`,
and what `build_host_artifacts` writes for install.py to consume.
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
    SnapshotPlan,
)

CONFIG = FileSpec("sanoid.conf", "/etc/sanoid/sanoid.conf", "644")
UNIT = FileSpec("homelab-zfs-snapshots.service", "/etc/systemd/system/x.service", "644")


def _artifacts(build_dir: Path) -> HostArtifacts:
    return HostArtifacts(build_dir=build_dir, file_specs=(CONFIG, UNIT))


def test_build_files_are_compared_against_their_remote_paths(tmp_path: Path) -> None:
    assert staging.diff_pairs_for(_artifacts(tmp_path)) == [
        (tmp_path / "sanoid.conf", "/etc/sanoid/sanoid.conf"),
        (tmp_path / "homelab-zfs-snapshots.service", "/etc/systemd/system/x.service"),
    ]


def test_build_dir_and_scripts_are_uploaded(tmp_path: Path) -> None:
    paths = staging.upload_paths_for(tmp_path / "zfs-automation", "ace", _artifacts(tmp_path))

    assert paths == [
        (tmp_path, f"{REMOTE_ROOT}/build/ace"),
        (tmp_path / "zfs-automation" / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]


@pytest.mark.parametrize("paused", [True, False])
def test_dry_run_reports_a_module_wide_pause_only_when_set(
    tmp_path: Path, monkeypatch, capsys, paused: bool
) -> None:
    monkeypatch.setattr(staging, "feature_paused", lambda *args, **kwargs: paused)
    (tmp_path / "sanoid.conf").write_text("x\n", encoding="utf-8")

    staging.report_dry_run(object(), "ace", _artifacts(tmp_path))

    output = capsys.readouterr().out
    assert ("Would pause zfs-automation on ace" in output) is paused
    assert "Would deploy zfs-automation to ace" in output
    assert "sanoid.conf" in output


# ---------------------------------------------------------------------------
# build_host_artifacts: the file map and the env, per host shape. These drive
# the flag matrix through a stub registry and assert the two outputs install.py
# actually consumes: file-map.conf and env.
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


@pytest.fixture
def zfs_build(monkeypatch: pytest.MonkeyPatch):
    """Build artifacts for a synthetic host, then remove its build dir.

    Real templates from the repo are used (they are the thing being rendered);
    only the inventory and the normalize layer are stubbed, so each test states
    exactly the host shape it is about.
    """
    build_dir = ROOT / "zfs-automation" / "build" / TEST_HOST

    def build(**shape):
        values = {
            "config.homelab_state_dir": "/var/lib/homelab",
            "zfs-automation.snapshot_schedule": "*-*-* 03:00:00",
            "zfs-automation.manage_snapshots": shape.get("manage_snapshots", True),
            "zfs-automation.manage_scrub": shape.get("manage_scrub", True),
        }

        class Registry:
            def get(self, _host: str, key: str, default: object = None) -> object:
                return values.get(key, default)

        monkeypatch.setattr(staging, "default_registry", lambda _root: Registry())
        monkeypatch.setattr(staging, "feature_paused", lambda *_a: shape.get("paused", False))
        monkeypatch.setattr(staging, "resolve_pools", lambda *_a: shape.get("pools", ["tank"]))
        monkeypatch.setattr(
            staging, "normalize_snapshot_plans", lambda *_a: shape.get("snapshot_plans", [PLAN])
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
    def test_a_host_gets_exactly_the_base_specs(self, zfs_build) -> None:
        assert zfs_build().file_specs == staging.BASE_FILE_SPECS

    def test_file_map_lists_every_spec_in_order(self, zfs_build) -> None:
        """install.py applies file-map.conf top to bottom, so order is contract."""
        artifacts = zfs_build()
        lines = (artifacts.build_dir / "file-map.conf").read_text(encoding="utf-8").splitlines()

        assert lines == [
            f"{spec.build_name}|{spec.remote_path}|{spec.mode}" for spec in artifacts.file_specs
        ]

    def test_every_mapped_file_is_built(self, zfs_build) -> None:
        artifacts = zfs_build()

        assert all((artifacts.build_dir / s.build_name).is_file() for s in artifacts.file_specs)

    def test_snapshot_timer_carries_the_schedule(self, zfs_build) -> None:
        timer = (zfs_build().build_dir / "homelab-zfs-snapshots.timer").read_text(
            encoding="utf-8"
        )

        assert "*-*-* 03:00:00" in timer


class TestBuildHostArtifactsEnv:
    def test_defaults_enable_snapshots_and_scrub(self, zfs_build) -> None:
        env = _env(zfs_build())

        assert env == {
            "HOMELAB_STATE_DIR": "/var/lib/homelab",
            "PAUSED": "false",
            "ENABLE_ZFS_SNAPSHOTS": "true",
            "ENABLE_ZFS_SCRUB": "true",
        }

    def test_manage_snapshots_false_disables_them_despite_declared_plans(
        self, zfs_build
    ) -> None:
        assert _env(zfs_build(manage_snapshots=False))["ENABLE_ZFS_SNAPSHOTS"] == "false"

    def test_no_declared_plans_disables_snapshots_even_when_managed(self, zfs_build) -> None:
        assert _env(zfs_build(snapshot_plans=[]))["ENABLE_ZFS_SNAPSHOTS"] == "false"

    def test_manage_scrub_false_or_no_pools_disables_scrub(self, zfs_build) -> None:
        assert _env(zfs_build(manage_scrub=False))["ENABLE_ZFS_SCRUB"] == "false"
        assert _env(zfs_build(pools=[]))["ENABLE_ZFS_SCRUB"] == "false"

    def test_module_wide_pause_is_a_single_flag_that_overrides_manage_flags(
        self, zfs_build
    ) -> None:
        """`paused` freezes the timers; it does not retract what is deployed."""
        env = _env(zfs_build(paused=True))

        assert env["PAUSED"] == "true"
        assert env["ENABLE_ZFS_SNAPSHOTS"] == "true"  # still installed, just stopped
