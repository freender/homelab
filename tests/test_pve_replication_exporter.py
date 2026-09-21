"""Behavioural tests for metrics-exporters' pve-replication-textfile-exporter.

This exporter exists to replace a one-shot PVE notification whose 12h tail kept
alerting long after replication recovered (homelab-ops#42), so the properties
worth pinning are the ones that decide whether the replacement alert can fire at
all, and whether it can fire wrongly:

  - The label names. `host` and `job` are both attached by the scrape config
    (`monitoring-config/configs/scrape.yml`), so either one written into the
    textfile is renamed `exported_*` on ingest. `source_node` and `job_id` exist
    precisely to avoid that, and the alert rule's `label_replace` of `host` from
    `target` depends on `target` being present and `source_node` surviving it.
  - `jobs_total` on a node with no jobs. ace is every job's target and osiris is
    standalone, so both legitimately report zero; that has to be a published 0
    rather than an empty file, or it is indistinguishable from a dead exporter.
  - `fail_count` is always emitted for an enabled job. A job dropping its series
    when healthy would make PveReplicationFailing unable to resolve.
  - A disabled job is not reported. PVE keeps the last `fail_count` on a job
    retired with `disable`, so counting it would alert forever on a job nobody
    runs.
  - A failed `pvesh` leaves the previous file alone rather than publishing
    "0 jobs, none failing" for a node whose state is unknown.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = (
    ROOT / "metrics-exporters" / "configs" / "common" / "pve-replication-textfile-exporter.py"
)

# bray, 2026-09-20, verbatim from `pvesh get /nodes/bray/replication`.
BRAY_JOBS = [
    {
        "duration": 2.704655,
        "fail_count": 0,
        "guest": 104,
        "id": "104-0",
        "jobnum": 0,
        "last_sync": 1789958702,
        "last_try": 1789958702,
        "next_sync": 1789959600,
        "schedule": "*/15",
        "source": "bray",
        "target": "ace",
        "type": "local",
        "vmtype": "lxc",
    },
    {
        "duration": 6.757043,
        "fail_count": 0,
        "guest": 108,
        "id": "108-1",
        "jobnum": 1,
        "last_sync": 1789958707,
        "last_try": 1789958707,
        "next_sync": 1789959600,
        "schedule": "*/15",
        "source": "bray",
        "target": "ace",
        "type": "local",
        "vmtype": "lxc",
    },
]


def _load():
    spec = importlib.util.spec_from_file_location("pve_replication_textfile_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return _load()


def series(text: str) -> dict[str, str]:
    """Metric line -> value, ignoring HELP/TYPE headers."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = value
    return out


def test_labels_avoid_the_names_the_scrape_config_owns(exporter):
    """`host` and `job` would both be renamed `exported_*` on ingest."""
    rendered = exporter.render(BRAY_JOBS, "bray")
    for line in rendered.splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        label_block = line[line.index("{") + 1 : line.rindex("}")]
        names = {pair.split("=", 1)[0] for pair in label_block.split(",")}
        assert "host" not in names
        assert "job" not in names
        assert {"job_id", "source_node", "target"} <= names


def test_source_node_and_target_are_both_published(exporter):
    """The alert rewrites `host` from `target` and keeps `source_node`; losing
    either would leave the alert unable to name the node at fault."""
    values = series(exporter.render(BRAY_JOBS, "bray"))
    key = (
        'homelab_pve_replication_fail_count{job_id="104-0",guest="104",'
        'source_node="bray",target="ace"}'
    )
    assert key in values


def test_healthy_job_still_publishes_fail_count(exporter):
    """A series that disappears when healthy can never resolve the alert."""
    values = series(exporter.render(BRAY_JOBS, "bray"))
    assert all(
        value == "0"
        for name, value in values.items()
        if name.startswith("homelab_pve_replication_fail_count{")
    )
    assert values["homelab_pve_replication_jobs_total"] == "2"


def test_failing_job_reports_its_count(exporter):
    jobs = [dict(BRAY_JOBS[0], fail_count=3)]
    values = series(exporter.render(jobs, "bray"))
    key = (
        'homelab_pve_replication_fail_count{job_id="104-0",guest="104",'
        'source_node="bray",target="ace"}'
    )
    assert values[key] == "3"


def test_timestamps_keep_full_precision(exporter):
    """`%g` renders 1789958702 as 1.78996e+09 -- 1789960000, about 22 minutes
    into the future. Every staleness query against this series would inherit
    that error, so the exact integer is pinned here."""
    values = series(exporter.render(BRAY_JOBS, "bray"))
    key = (
        'homelab_pve_replication_last_sync_timestamp_seconds{job_id="104-0",'
        'guest="104",source_node="bray",target="ace"}'
    )
    assert values[key] == "1789958702"
    assert "e+" not in exporter.render(BRAY_JOBS, "bray")


def test_duration_keeps_its_fractional_part(exporter):
    values = series(exporter.render(BRAY_JOBS, "bray"))
    key = (
        'homelab_pve_replication_duration_seconds{job_id="108-1",guest="108",'
        'source_node="bray",target="ace"}'
    )
    assert values[key] == "6.757043"


def test_node_with_no_jobs_publishes_an_explicit_zero(exporter):
    """ace (every job's target) and osiris (standalone) both return []."""
    values = series(exporter.render([], "ace"))
    assert values["homelab_pve_replication_jobs_total"] == "0"


def test_disabled_job_is_not_reported(exporter):
    """PVE keeps the stale fail_count on a disabled job, so counting it would
    alert forever on a job nobody runs."""
    jobs = [dict(BRAY_JOBS[0], disable=1, fail_count=9), BRAY_JOBS[1]]
    values = series(exporter.render(jobs, "bray"))
    assert values["homelab_pve_replication_jobs_total"] == "1"
    assert not any("104-0" in name for name in values)


def test_never_synced_job_omits_last_sync_rather_than_claiming_the_epoch(exporter):
    jobs = [dict(BRAY_JOBS[0], last_sync=0)]
    values = series(exporter.render(jobs, "bray"))
    assert not any(name.startswith("homelab_pve_replication_last_sync") for name in values)
    assert any(name.startswith("homelab_pve_replication_fail_count") for name in values)


def test_quotes_in_a_label_value_cannot_break_the_textfile(exporter):
    jobs = [dict(BRAY_JOBS[0], target='ace"evil')]
    rendered = exporter.render(jobs, "bray")
    assert r'target="ace\"evil"' in rendered


def test_pvesh_failure_leaves_the_previous_file_untouched(exporter, tmp_path, monkeypatch):
    """Publishing "0 jobs, none failing" for a node whose state is unknown is
    the one answer that must never be synthesised."""
    out_file = tmp_path / "pve-replication.prom"
    out_file.write_text("homelab_pve_replication_jobs_total 2\n", encoding="utf-8")
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", out_file)
    monkeypatch.setattr(
        exporter,
        "read_jobs",
        lambda node: (_ for _ in ()).throw(exporter.ReplicationError("pmxcfs unavailable")),
    )

    assert exporter.main() == 1
    assert out_file.read_text(encoding="utf-8") == "homelab_pve_replication_jobs_total 2\n"


def test_nonzero_pvesh_exit_is_an_error_not_an_empty_job_list(exporter, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 2, stdout="", stderr="no such node"),
    )
    with pytest.raises(exporter.ReplicationError, match="no such node"):
        exporter.read_jobs("bray")


def test_valid_pvesh_output_is_parsed(exporter, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(BRAY_JOBS)),
    )
    assert exporter.read_jobs("bray") == BRAY_JOBS


def test_non_list_pvesh_output_is_refused(exporter, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout='{"id": "104-0"}'),
    )
    with pytest.raises(exporter.ReplicationError, match="expected a list"):
        exporter.read_jobs("bray")


def test_written_file_is_world_readable(exporter, tmp_path, monkeypatch):
    """node_exporter reads the textfile dir as its own user, not root."""
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", tmp_path / "pve-replication.prom")
    exporter.write(exporter.render(BRAY_JOBS, "bray"))
    assert (tmp_path / "pve-replication.prom").stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".pve-replication.prom.*"))
