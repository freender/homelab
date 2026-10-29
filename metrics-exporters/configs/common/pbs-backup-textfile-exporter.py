#!/usr/bin/env python3
"""Write PBS backup freshness and job state for the node_exporter textfile collector.

Before this the only backup signal was PBS's own notification webhook, which
fires on a job that *fails* and says nothing about one that never runs: a
vzdump job deleted or disabled on the PVE side, a client timer stopped on a
host, or a sync job whose schedule was cleared all produced silence. A stale
backup is a condition, not an event, so it needs a metric.

Everything is read through `proxmox-backup-debug api get`, which calls the
local API handlers in-process as root -- no credential, no network, and the
same JSON the web UI renders:

  * /admin/datastore                       -> datastores
  * /admin/datastore/<store>/namespace     -> namespaces (root is "")
  * /admin/datastore/<store>/groups?ns=... -> `last-backup` per group
  * /admin/{sync,verify,prune,gc}          -> `last-run-state`/`-endtime`

Sync jobs are listed twice, once per direction: `/admin/sync` defaults to pull
only, which is also why `proxmox-backup-manager sync-job list` hides push jobs.

Label notes, the same constraints as pve-replication-textfile-exporter:

  * No `host` and no `job` label -- the scrape config owns both and either
    would be renamed `exported_*` on ingest. Jobs are `job_type` + `job_id`.
  * Namespace is `ns`, with the root namespace published as `ns=""`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
OUT_FILE = OUT_DIR / "pbs-backups.prom"

DEBUG_BIN = os.environ.get("PBS_DEBUG_BIN", "/usr/sbin/proxmox-backup-debug")

# Each call is a local API handler and returns in well under a second; the
# timeout only exists so a wedged datastore lock loses to the next timer tick.
API_TIMEOUT = 60

# (job_type, API path, extra args). GC is per datastore, keyed by `store`.
JOB_ENDPOINTS = (
    ("sync", "/admin/sync", ("--sync-direction", "pull")),
    ("sync", "/admin/sync", ("--sync-direction", "push")),
    ("verify", "/admin/verify", ()),
    ("prune", "/admin/prune", ()),
    ("gc", "/admin/gc", ()),
)

HEADERS = (
    "# HELP homelab_pbs_groups_total Backup groups per datastore namespace.",
    "# TYPE homelab_pbs_groups_total gauge",
    "# HELP homelab_pbs_group_last_backup_timestamp_seconds Unix time of the "
    "newest snapshot in the backup group.",
    "# TYPE homelab_pbs_group_last_backup_timestamp_seconds gauge",
    "# HELP homelab_pbs_group_snapshots Snapshots currently held in the backup group.",
    "# TYPE homelab_pbs_group_snapshots gauge",
    "# HELP homelab_pbs_job_last_run_ok 1 if the job's last finished run "
    "ended OK, 0 otherwise; absent while it has never finished.",
    "# TYPE homelab_pbs_job_last_run_ok gauge",
    "# HELP homelab_pbs_job_last_run_timestamp_seconds Unix time the job's "
    "last run finished.",
    "# TYPE homelab_pbs_job_last_run_timestamp_seconds gauge",
    "# HELP homelab_pbs_job_running 1 while a run of the job is in progress.",
    "# TYPE homelab_pbs_job_running gauge",
)


class PbsError(Exception):
    pass


def api_get(path: str, *args: str) -> list:
    command = [DEBUG_BIN, "api", "get", path, *args, "--output-format", "json"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=API_TIMEOUT, check=False
        )
    except OSError as exc:
        raise PbsError(f"could not run {DEBUG_BIN}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PbsError(f"{path} timed out after {API_TIMEOUT}s") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PbsError(f"api get {path} failed: {detail}")
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PbsError(f"{path} returned invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise PbsError(f"{path}: expected a list, got {type(data).__name__}")
    return data


def collect_groups() -> list[tuple[str, str, list[dict]]]:
    """`(store, ns, groups)` for every namespace of every datastore."""
    collected = []
    for datastore in api_get("/admin/datastore"):
        store = str(datastore["store"])
        for entry in api_get(f"/admin/datastore/{store}/namespace"):
            ns = str(entry.get("ns", ""))
            args = ("--ns", ns) if ns else ()
            collected.append((store, ns, api_get(f"/admin/datastore/{store}/groups", *args)))
    return collected


def collect_jobs() -> list[tuple[str, dict]]:
    return [
        (job_type, job)
        for job_type, path, args in JOB_ENDPOINTS
        for job in api_get(path, *args)
    ]


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def label_block(pairs: tuple[tuple[str, str], ...]) -> str:
    return ",".join(f'{key}="{escape(value)}"' for key, value in pairs)


def fmt(value: float) -> str:
    """Full precision; `%g` would round a Unix timestamp by ~1000 seconds."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def group_lines(store: str, ns: str, groups: list[dict]) -> list[str]:
    ns_labels = label_block((("datastore", store), ("ns", ns)))
    lines = [f"homelab_pbs_groups_total{{{ns_labels}}} {len(groups)}"]
    for group in sorted(groups, key=lambda g: (str(g.get("backup-type")), str(g.get("backup-id")))):
        labels = label_block(
            (
                ("datastore", store),
                ("ns", ns),
                ("backup_type", str(group.get("backup-type", ""))),
                ("backup_id", str(group.get("backup-id", ""))),
            )
        )
        last = group.get("last-backup")
        # A group whose only snapshot is still being written has no
        # last-backup yet; publishing 0 would read as "stale since 1970".
        if last:
            lines.append(f"homelab_pbs_group_last_backup_timestamp_seconds{{{labels}}} {fmt(last)}")
        lines.append(f"homelab_pbs_group_snapshots{{{labels}}} {fmt(group.get('backup-count', 0))}")
    return lines


def job_lines(job_type: str, job: dict) -> list[str]:
    job_id = str(job.get("id") or job.get("store", ""))
    labels = label_block(
        (("job_type", job_type), ("job_id", job_id), ("datastore", str(job.get("store", ""))))
    )
    state = job.get("last-run-state")
    endtime = job.get("last-run-endtime")
    # PBS drops both fields while a run is in progress, so "running" is read
    # from their absence alongside an upid that says the job has run before.
    running = state is None and bool(job.get("last-run-upid") or job.get("upid"))
    lines = [f"homelab_pbs_job_running{{{labels}}} {1 if running else 0}"]
    if state is not None:
        lines.append(f"homelab_pbs_job_last_run_ok{{{labels}}} {1 if state == 'OK' else 0}")
    if endtime:
        lines.append(f"homelab_pbs_job_last_run_timestamp_seconds{{{labels}}} {fmt(endtime)}")
    return lines


def render(groups: list[tuple[str, str, list[dict]]], jobs: list[tuple[str, dict]]) -> str:
    lines = list(HEADERS)
    for store, ns, entries in sorted(groups, key=lambda item: (item[0], item[1])):
        lines.extend(group_lines(store, ns, entries))
    for job_type, job in jobs:
        lines.extend(job_lines(job_type, job))
    return "\n".join(lines) + "\n"


def write(content: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, prefix=".pbs-backups.prom.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, OUT_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise


def main() -> int:
    try:
        groups = collect_groups()
        jobs = collect_jobs()
    except (PbsError, KeyError) as exc:
        # Leave the previous .prom alone: an empty or partial file would read
        # as "no groups" -- the one answer that must never be synthesised.
        # The stale-file rule catches a file that stops being rewritten.
        print(f"pbs-backup-textfile-exporter: {exc}", file=sys.stderr)
        return 1

    write(render(groups, jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
