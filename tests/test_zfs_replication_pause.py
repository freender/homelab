from __future__ import annotations

from pathlib import Path

from homelab.hosts import HostRegistry
from homelab.modules.zfs_automation import normalize_replication_config


def _registry(tmp_path: Path) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(
        """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features:
    zfs-automation:
      replication_jobs:
        alpha:
          schedule: '*-*-* 02:00:00'
          plans:
            - source: tank/a
              target: remote:tank/a
        beta:
          paused: true
          plans:
            - source: tank/b
              target: remote:tank/b
        gamma:
          enabled: false
          plans:
            - source: tank/c
              target: remote:tank/c
""".lstrip(),
        encoding="utf-8",
    )
    return HostRegistry(path)


def test_paused_job_stays_deployed_but_marked(tmp_path: Path) -> None:
    jobs = {job.name: job for job in normalize_replication_config(_registry(tmp_path), "ace")}

    # enabled:false job is retired (excluded); paused + normal jobs remain.
    assert set(jobs) == {"alpha", "beta"}
    assert jobs["alpha"].paused is False
    assert jobs["beta"].paused is True


def test_enabled_false_job_still_retired_and_included_when_requested(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    # Default: disabled job dropped.
    default_names = {job.name for job in normalize_replication_config(registry, "ace")}
    assert "gamma" not in default_names

    # include_disabled surfaces it; it is not paused (it is retired).
    all_jobs = {
        job.name: job
        for job in normalize_replication_config(registry, "ace", include_disabled=True)
    }
    assert set(all_jobs) == {"alpha", "beta", "gamma"}
    assert all_jobs["gamma"].paused is False
