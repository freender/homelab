#!/usr/bin/env python3
"""Write PVE storage-replication state for the node_exporter textfile collector.

Replication had no state-based signal at all before this (homelab-ops#42):
VictoriaMetrics carried zero `pve_*` series and nothing matching `replicat`, so
the *only* evidence a job was failing was PVE's one-shot notification. That is an
email-style event, not a condition -- `PVE/API2/Replication.pm` contains exactly
one notification call, `PVE::Notify::error("replication", ...)`, with no `info`/
recovery counterpart anywhere in the code path. Nothing on the Proxmox side ever
says a job recovered.

Two consequences followed from that, and both are what this exporter exists to
end:

  * The event was posted to Alertmanager without an `endsAt`, so it expired on
    `resolve_timeout` (12h) rather than on the condition clearing. A four-minute
    ace reboot on 2026-09-20 left two `critical` alerts standing for twelve
    hours after replication was verifiably healthy again.
  * An event has no duration, so there is no way to express "ignore a failure
    shorter than a reboot". A metric has one: the rule that reads this can carry
    a `for:`, which is the whole mechanism that separates a rebooting target
    from a genuinely broken job.

Source is `pvesh get /nodes/<node>/replication`, which reports only the jobs
whose source is this node -- exactly the jobs it is responsible for running. It
returns `[]` on a node with no jobs (ace, which is every job's *target*, and
standalone osiris), so the exporter is safe to deploy to every PVE node and
`homelab_pve_replication_jobs_total 0` is a real answer rather than an error.

Label notes, both of which are load-bearing:

  * No `host` label is written. The scrape target already supplies `host`
    (`monitoring-config/configs/scrape.yml`), and a `host` in the textfile would
    be renamed `exported_host` on ingest -- which is where the existing
    `homelab_zpool_*` series' redundant `exported_host` comes from. The node
    running the job is published as `source_node` instead, where nothing
    clobbers it and the alert rule can keep it after rewriting `host` to the
    target.
  * `job_id`, never `job`. `job` is the scrape job name and would be renamed
    `exported_job` the same way.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
OUT_FILE = OUT_DIR / "pve-replication.prom"

PVESH = os.environ.get("PVESH_BIN", "/usr/bin/pvesh")

# `pvesh` can block on pmxcfs; the timer runs every minute, so a hung call must
# lose to the next one rather than pile up.
PVESH_TIMEOUT = 30

HEADERS = (
    "# HELP homelab_pve_replication_jobs_total Number of enabled PVE replication "
    "jobs whose source is this node.",
    "# TYPE homelab_pve_replication_jobs_total gauge",
    "# HELP homelab_pve_replication_fail_count Consecutive failures for this "
    "replication job; 0 when the last run succeeded.",
    "# TYPE homelab_pve_replication_fail_count gauge",
    "# HELP homelab_pve_replication_last_sync_timestamp_seconds Unix time of the "
    "last successful sync; absent when the job has never synced.",
    "# TYPE homelab_pve_replication_last_sync_timestamp_seconds gauge",
    "# HELP homelab_pve_replication_last_try_timestamp_seconds Unix time of the "
    "last attempt, successful or not.",
    "# TYPE homelab_pve_replication_last_try_timestamp_seconds gauge",
    "# HELP homelab_pve_replication_duration_seconds Duration of the last "
    "replication run.",
    "# TYPE homelab_pve_replication_duration_seconds gauge",
)


class ReplicationError(Exception):
    pass


def nodename() -> str:
    """The PVE node name, which is the short hostname (PVE::INotify::nodename)."""
    return socket.gethostname().split(".")[0]


def read_jobs(node: str) -> list[dict]:
    try:
        result = subprocess.run(
            [PVESH, "get", f"/nodes/{node}/replication", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=PVESH_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise ReplicationError(f"could not run {PVESH}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReplicationError(f"{PVESH} timed out after {PVESH_TIMEOUT}s") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ReplicationError(f"pvesh get /nodes/{node}/replication failed: {detail}")

    try:
        jobs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReplicationError(f"pvesh returned invalid JSON: {exc}") from exc
    if not isinstance(jobs, list):
        raise ReplicationError(f"expected a list of jobs, got {type(jobs).__name__}")
    return jobs


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def labels(job: dict, source_node: str) -> str:
    pairs = (
        ("job_id", str(job.get("id", ""))),
        ("guest", str(job.get("guest", ""))),
        ("source_node", source_node),
        ("target", str(job.get("target", ""))),
    )
    return ",".join(f'{key}="{escape(value)}"' for key, value in pairs)


def is_enabled(job: dict) -> bool:
    """A job retired with `disable` should not be reported as failing.

    PVE spells the flag `disable` and omits it entirely when unset, so absence
    means enabled. Checked rather than assumed: a disabled job keeps its last
    fail_count, so counting it would alert forever on a job nobody runs.
    """
    return str(job.get("disable", 0)) not in ("1", "true", "True")


def number(job: dict, key: str) -> float | None:
    value = job.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt(value: float) -> str:
    """Render a value at full precision.

    Not `%g`, which carries six significant digits and so rounds a Unix
    timestamp to the nearest ~1000 seconds -- last_sync 1789958702 came out as
    1.78996e+09 (1789960000), about 22 minutes into the future. The staleness
    queries this series exists to answer are exactly the ones that would have
    been wrong.
    """
    if value.is_integer():
        return str(int(value))
    return repr(value)


def render(jobs: list[dict], source_node: str) -> str:
    lines = list(HEADERS)
    enabled = [job for job in jobs if is_enabled(job)]
    lines.append(f"homelab_pve_replication_jobs_total {len(enabled)}")

    for job in sorted(enabled, key=lambda entry: str(entry.get("id", ""))):
        label_set = labels(job, source_node)
        # fail_count is the alerting series, so it is always emitted -- a job
        # present but missing the key is reported as 0 rather than dropped,
        # which would make a healthy job and a vanished one look identical.
        fail_count = number(job, "fail_count") or 0
        lines.append(f"homelab_pve_replication_fail_count{{{label_set}}} {fmt(fail_count)}")

        # last_sync is 0 or absent on a job that has never completed once. An
        # explicit 0 would read as "synced at the epoch" and make any age
        # calculation nonsense, so the series is omitted instead.
        last_sync = number(job, "last_sync")
        if last_sync:
            lines.append(
                f"homelab_pve_replication_last_sync_timestamp_seconds{{{label_set}}} "
                f"{fmt(last_sync)}"
            )
        last_try = number(job, "last_try")
        if last_try:
            lines.append(
                f"homelab_pve_replication_last_try_timestamp_seconds{{{label_set}}} "
                f"{fmt(last_try)}"
            )
        duration = number(job, "duration")
        if duration is not None:
            lines.append(
                f"homelab_pve_replication_duration_seconds{{{label_set}}} {fmt(duration)}"
            )

    return "\n".join(lines) + "\n"


def write(content: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, prefix=".pve-replication.prom.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, OUT_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise


def main() -> int:
    node = nodename()
    try:
        jobs = read_jobs(node)
    except ReplicationError as exc:
        # Deliberately leaves the previous .prom in place rather than writing an
        # empty one: node_exporter would otherwise publish "0 jobs, none
        # failing" for a node whose real state is unknown, which is the one
        # answer that must never be synthesised. The failed unit is itself
        # alerted on by SystemdUnitFailed.
        print(f"{node}: {exc}", file=sys.stderr)
        return 1

    write(render(jobs, node))
    return 0


if __name__ == "__main__":
    sys.exit(main())
