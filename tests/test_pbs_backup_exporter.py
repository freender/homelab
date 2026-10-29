"""Behavioural tests for metrics-exporters' pbs-backup-textfile-exporter.

The properties pinned here are the ones that decide whether PbsBackupStale and
PbsJobStale can fire, and whether they can fire wrongly:

  - No `host` or `job` label; the scrape config owns both.
  - A group with no finished snapshot omits last_backup rather than claiming
    the epoch, which would read as 56 years stale.
  - A running job reports running=1 and drops last-run fields, because PBS
    removes `last-run-state`/`-endtime` for the duration of a run.
  - Both sync directions are queried: `/admin/sync` defaults to pull only.
  - An API failure leaves the previous file alone instead of publishing
    "no groups".
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "pbs-backup-textfile-exporter.py"

# xur, 2026-10-28, from `proxmox-backup-debug api get ...`.
GROUPS = [
    {"backup-count": 10, "backup-id": "101", "backup-type": "ct", "last-backup": 1793156404},
    {"backup-count": 13, "backup-id": "ghost", "backup-type": "host", "last-backup": 1793197499},
]
PUSH_DONE = {
    "id": "sync-offsite-cinci-push",
    "last-run-endtime": 1793217455,
    "last-run-state": "OK",
    "last-run-upid": "UPID:xur:...:syncjob:...",
    "store": "backup",
    "sync-direction": "push",
}
PUSH_RUNNING = {
    "id": "sync-offsite-cottonwood-push",
    "last-run-upid": "UPID:xur:...:syncjob:...",
    "store": "backup",
    "sync-direction": "push",
}
GC = {
    "last-run-endtime": 1793160026,
    "last-run-state": "OK",
    "store": "backup",
    "upid": "UPID:xur:...:garbage_collection:backup:root@pam:",
}


def _load():
    spec = importlib.util.spec_from_file_location("pbs_backup_textfile_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return _load()


def series(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = value
    return out


def rendered(exporter, groups=None, jobs=None) -> str:
    return exporter.render(
        [("backup", "freender", GROUPS if groups is None else groups)],
        [("sync", PUSH_DONE), ("sync", PUSH_RUNNING), ("gc", GC)] if jobs is None else jobs,
    )


def test_labels_avoid_the_names_the_scrape_config_owns(exporter):
    for line in rendered(exporter).splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        block = line[line.index("{") + 1 : line.rindex("}")]
        names = {pair.split("=", 1)[0] for pair in block.split(",")}
        assert "host" not in names
        assert "job" not in names


def test_group_freshness_keeps_full_precision(exporter):
    values = series(rendered(exporter))
    key = (
        'homelab_pbs_group_last_backup_timestamp_seconds{datastore="backup",'
        'ns="freender",backup_type="ct",backup_id="101"}'
    )
    assert values[key] == "1793156404"
    assert values['homelab_pbs_groups_total{datastore="backup",ns="freender"}'] == "2"


def test_empty_namespace_publishes_an_explicit_zero(exporter):
    values = series(rendered(exporter, groups=[]))
    assert values['homelab_pbs_groups_total{datastore="backup",ns="freender"}'] == "0"


def test_group_without_a_finished_snapshot_omits_last_backup(exporter):
    groups = [{"backup-count": 0, "backup-id": "113", "backup-type": "vm"}]
    values = series(rendered(exporter, groups=groups))
    assert not any(name.startswith("homelab_pbs_group_last_backup") for name in values)
    assert any(name.startswith("homelab_pbs_group_snapshots") for name in values)


def test_finished_job_reports_ok_and_endtime(exporter):
    values = series(rendered(exporter))
    labels = '{job_type="sync",job_id="sync-offsite-cinci-push",datastore="backup"}'
    assert values[f"homelab_pbs_job_last_run_ok{labels}"] == "1"
    assert values[f"homelab_pbs_job_last_run_timestamp_seconds{labels}"] == "1793217455"
    assert values[f"homelab_pbs_job_running{labels}"] == "0"


def test_running_job_is_flagged_and_publishes_no_stale_endtime(exporter):
    values = series(rendered(exporter))
    labels = '{job_type="sync",job_id="sync-offsite-cottonwood-push",datastore="backup"}'
    assert values[f"homelab_pbs_job_running{labels}"] == "1"
    assert f"homelab_pbs_job_last_run_ok{labels}" not in values
    assert f"homelab_pbs_job_last_run_timestamp_seconds{labels}" not in values


def test_failed_job_reports_zero(exporter):
    failed = dict(PUSH_DONE, **{"last-run-state": "Sync failed with some errors!"})
    values = series(rendered(exporter, jobs=[("sync", failed)]))
    labels = '{job_type="sync",job_id="sync-offsite-cinci-push",datastore="backup"}'
    assert values[f"homelab_pbs_job_last_run_ok{labels}"] == "0"


def test_gc_is_keyed_by_its_datastore(exporter):
    values = series(rendered(exporter))
    labels = '{job_type="gc",job_id="backup",datastore="backup"}'
    assert values[f"homelab_pbs_job_last_run_ok{labels}"] == "1"


def test_never_run_job_is_not_running(exporter):
    job = {"id": "v-new", "store": "backup"}
    values = series(rendered(exporter, jobs=[("verify", job)]))
    labels = '{job_type="verify",job_id="v-new",datastore="backup"}'
    assert values[f"homelab_pbs_job_running{labels}"] == "0"


def test_both_sync_directions_are_queried(exporter, monkeypatch):
    seen = []
    monkeypatch.setattr(exporter, "api_get", lambda path, *args: seen.append((path, args)) or [])
    exporter.collect_jobs()
    assert ("/admin/sync", ("--sync-direction", "pull")) in seen
    assert ("/admin/sync", ("--sync-direction", "push")) in seen


def test_root_namespace_is_queried_without_ns_argument(exporter, monkeypatch):
    calls = []

    def fake(path, *args):
        calls.append((path, args))
        if path == "/admin/datastore":
            return [{"store": "backup"}]
        if path.endswith("/namespace"):
            return [{"ns": ""}, {"ns": "freender"}]
        return []

    monkeypatch.setattr(exporter, "api_get", fake)
    assert exporter.collect_groups() == [("backup", "", []), ("backup", "freender", [])]
    assert ("/admin/datastore/backup/groups", ()) in calls
    assert ("/admin/datastore/backup/groups", ("--ns", "freender")) in calls


def test_api_failure_leaves_the_previous_file_untouched(exporter, tmp_path, monkeypatch):
    out_file = tmp_path / "pbs-backups.prom"
    out_file.write_text("homelab_pbs_groups_total 3\n", encoding="utf-8")
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", out_file)
    monkeypatch.setattr(
        exporter,
        "collect_groups",
        lambda: (_ for _ in ()).throw(exporter.PbsError("datastore locked")),
    )
    assert exporter.main() == 1
    assert out_file.read_text(encoding="utf-8") == "homelab_pbs_groups_total 3\n"


def test_nonzero_exit_is_an_error(exporter, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 2, stdout="", stderr="no such datastore"),
    )
    with pytest.raises(exporter.PbsError, match="no such datastore"):
        exporter.api_get("/admin/datastore")


def test_non_list_output_is_refused(exporter, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=json.dumps({"store": "x"})),
    )
    with pytest.raises(exporter.PbsError, match="expected a list"):
        exporter.api_get("/admin/datastore")


def test_written_file_is_world_readable(exporter, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", tmp_path / "pbs-backups.prom")
    exporter.write(rendered(exporter))
    assert (tmp_path / "pbs-backups.prom").stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".pbs-backups.prom.*"))
