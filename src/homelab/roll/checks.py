"""Pre-flight and recovery checks, as findings. Pure: facts in, findings out.

The stop conditions are the runbook's (pve-upgrade/README.md, "Rolling reboot
runbook"); this file is where they are enforced, so the README names them and
does not restate the commands behind them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .facts import NodeFacts, VmFacts
from .parse import POOLS_HEALTHY, AptChanges, HaStatus, Kernel, ReplJob, SwarmNode

STOP = "stop"
WARN = "warn"
NOTE = "note"

LRM_OK = frozenset({"active", "idle"})
HA_SETTLED = frozenset({"started", "stopped", "disabled", "ignored"})
HA_FAILED = frozenset({"error", "fence", "recovery"})
# Every replication schedule in hosts.conf is hourly or faster.
REPLICATION_STALE = timedelta(hours=2)
# PVE refreshes its lists daily (the aptupdate task), as does apt-daily elsewhere.
LISTS_STALE = timedelta(hours=48)


@dataclass(frozen=True)
class Finding:
    level: str
    host: str
    message: str


def stop(host: str, message: str) -> Finding:
    return Finding(STOP, host, message)


def kernel_reading(host: str, kernel: Kernel) -> list[Finding]:
    if kernel.required is None:
        return [stop(host, "the reboot exporter has no reading (reboot.prom)")]
    return []


def quorum(facts: NodeFacts) -> list[Finding]:
    if facts.standalone:
        return []
    q = facts.quorum
    if not q.quorate:
        return [stop(facts.host, "cluster is not quorate")]
    if q.total is None or q.expected is None or q.total < q.expected:
        return [stop(facts.host, f"cluster has {q.total} of {q.expected} expected votes")]
    return []


def replication(facts: NodeFacts) -> list[Finding]:
    if facts.replication and facts.clock is None:
        return [stop(facts.host, "cannot read the node clock to age replication")]
    findings: list[Finding] = []
    for job in facts.replication:
        problem = replication_problem(job, facts.clock)
        if problem:
            findings.append(stop(facts.host, f"replication {job.job} -> {job.target}: {problem}"))
    return findings


def replication_problem(job: ReplJob, clock: datetime | None) -> str | None:
    if not job.enabled:
        return None
    if job.state != "OK" or job.fail_count:
        return f"{job.state}, {job.fail_count} failure(s)"
    if job.last_sync is None or clock is None:
        return "never synced"
    age = clock - job.last_sync
    if age > REPLICATION_STALE:
        return f"last sync {age} ago"
    return None


def pools(host: str, text: str) -> list[Finding]:
    if text.strip() != POOLS_HEALTHY:
        first = text.strip().splitlines()[0] if text.strip() else "no output"
        return [stop(host, f"zpool status -x: {first}")]
    return []


def boot(facts: NodeFacts) -> list[Finding]:
    entries = facts.boot
    if entries.first_loadable is None or entries.dead is None:
        return [stop(facts.host, "the boot-entry exporter has no reading; bootability unknown")]
    if not entries.first_loadable:
        return [stop(facts.host, "first UEFI boot entry cannot load; fix it with efibootmgr first")]
    if entries.dead:
        return [stop(facts.host, f"{entries.dead} dead UEFI boot entry(ies); clean them first")]
    return []


def ups(facts: NodeFacts) -> list[Finding]:
    if facts.ups is not None and not facts.ups.startswith("ONLINE"):
        return [stop(facts.host, f"UPS status is {facts.ups}")]
    return []


def tasks(host: str, running: list[str] | None, owner: str | None = None) -> list[Finding]:
    where = f" on {owner}" if owner else ""
    if running is None:
        return [stop(host, f"cannot read running tasks{where}")]
    if running:
        return [stop(host, f"task(s) running{where}: {', '.join(running)}")]
    return []


def upgrade_unit(host: str, unit: dict[str, str]) -> list[Finding]:
    """The automation that installs kernels must have succeeded last time."""
    if unit.get("LoadState") == "not-found":
        return [Finding(WARN, host, "no homelab-apt-dist-upgrade unit")]
    if unit.get("Result") != "success":
        return [stop(host, f"last homelab-apt-dist-upgrade result: {unit.get('Result')}")]
    return []


def available(host: str, apt: AptChanges | None, kernel: Kernel, remedy: str) -> list[Finding]:
    """A kernel released since the last upgrade means rebooting twice: install it first.

    Anything else pending does not change what the reboot boots into, so it only warns.
    """
    if apt is None:
        return [stop(host, "cannot read the apt-get -s dist-upgrade summary")]
    newer = [release for release in apt.kernels if release != kernel.installed]
    if newer:
        return [
            stop(
                host,
                f"kernel {', '.join(newer)} is available but not installed: a reboot now "
                f"boots {kernel.installed or '?'} and needs another once it lands. "
                f"Install it first ({remedy}), then plan again",
            )
        ]
    if apt.total:
        return [Finding(WARN, host, f"{apt.total} non-kernel package change(s) available")]
    return []


def lists(host: str, lists_age: float | None) -> list[Finding]:
    """Stale package lists hide a released kernel from the check above."""
    if lists_age is None:
        return [Finding(WARN, host, "cannot age the apt package lists")]
    if lists_age > LISTS_STALE.total_seconds():
        hours = int(lists_age // 3600)
        return [Finding(WARN, host, f"apt package lists are {hours} h old; a new kernel may hide")]
    return []


def upgrade(
    host: str,
    unit: dict[str, str],
    apt: AptChanges | None,
    kernel: Kernel,
    lists_age: float | None,
    remedy: str,
) -> list[Finding]:
    return [
        *upgrade_unit(host, unit),
        *available(host, apt, kernel, remedy),
        *lists(host, lists_age),
    ]


def node_preflight(facts: NodeFacts, remedy: str) -> list[Finding]:
    return [
        *kernel_reading(facts.host, facts.kernel),
        *quorum(facts),
        *replication(facts),
        *pools(facts.host, facts.pools),
        *boot(facts),
        *ups(facts),
        *tasks(facts.host, facts.tasks),
        *upgrade(facts.host, facts.upgrade, facts.apt, facts.kernel, facts.lists_age, remedy),
    ]


def ha(host: str, status: HaStatus) -> list[Finding]:
    """Cluster HA health. Unsettled services are a stop too: wait, then re-run."""
    if not status.quorum_ok:
        return [stop(host, "ha-manager does not report quorum OK")]
    findings = [
        stop(host, f"HA LRM {node} is {state}")
        for node, state in sorted(status.lrms.items())
        if state not in LRM_OK
    ]
    for service in status.services.values():
        if service.state in HA_FAILED:
            findings.append(stop(host, f"HA {service.sid} is {service.state} on {service.node}"))
        elif service.state not in HA_SETTLED:
            where = f"{service.state} on {service.node}"
            findings.append(stop(host, f"HA {service.sid} is still settling ({where})"))
    return findings


def ci_jobs(facts: VmFacts) -> list[Finding]:
    if facts.ci_jobs:
        return [stop(facts.host, f"{facts.ci_jobs} CI job(s) running on its runners")]
    return []


def healthy_manager(node: SwarmNode) -> bool:
    return node.status == "Ready" and node.manager in ("Reachable", "Leader")


def swarm_peers(facts: VmFacts) -> list[Finding]:
    """A swarm manager may only go down while every other manager is healthy."""
    managers = [node for node in facts.swarm if node.manager]
    if not any(node.is_self for node in managers):
        return []
    sick = [
        f"{node.hostname} ({node.status}, {node.manager})"
        for node in managers
        if not node.is_self and not healthy_manager(node)
    ]
    if sick:
        return [stop(facts.host, f"swarm manager(s) not healthy: {', '.join(sick)}")]
    return []


def vips(facts: VmFacts, addresses: list[str]) -> list[Finding]:
    held = [ip for ip in addresses if f" {ip}/" in f" {facts.addresses}"]
    return [Finding(NOTE, facts.host, f"holds VIP {ip}; it fails over (expected)") for ip in held]


def vm_preflight(facts: VmFacts, vip_addresses: list[str], remedy: str) -> list[Finding]:
    return [
        *kernel_reading(facts.host, facts.kernel),
        *upgrade(facts.host, facts.upgrade, facts.apt, facts.kernel, facts.lists_age, remedy),
        *ci_jobs(facts),
        *swarm_peers(facts),
        *vips(facts, vip_addresses),
    ]


# --------------------------------------------------------------------------------------
# After the reboot
# --------------------------------------------------------------------------------------


def new_kernel(host: str, uname: str, kernel: Kernel) -> list[Finding]:
    """Compare against the installed label, not the flag.

    The exporter refreshes 2 min after boot, so right after a reboot reboot.prom
    still holds the pre-reboot `homelab_reboot_required 1`. The newest installed
    release does not change across the reboot, so `uname -r` against it is the
    answer at any point after boot.
    """
    if not kernel.installed:
        return [stop(host, "the reboot exporter has no installed-kernel reading")]
    if uname.strip() != kernel.installed:
        return [stop(host, f"running {uname.strip() or '?'}, installed {kernel.installed}")]
    return []


def scraped(host: str, values: list[float] | None, monitor: str | None) -> list[Finding]:
    """Fail closed: a monitor that cannot answer is not a host that is scraped."""
    if monitor is None:
        return [stop(host, "no docker-stacks host runs victoria-metrics: cannot check up")]
    if values is None:
        return [stop(host, f"could not ask {monitor} for up{{host}}")]
    if not values:
        return [stop(host, "no up series: monitoring is not scraping it")]
    if min(values) < 1:
        return [stop(host, "up is 0 for at least one scrape job")]
    return []


def monitor_ready(monitor: str, codes: dict[str, str]) -> list[Finding]:
    """HTTP status per readiness endpoint on the metrics host; only 200 is ready."""
    return [
        stop(monitor, f"{name} is not ready (HTTP {code or '?'})")
        for name, code in codes.items()
        if code != "200"
    ]


def node_recovered(facts: NodeFacts) -> list[Finding]:
    findings = [
        *new_kernel(facts.host, facts.uname, facts.kernel),
        *pools(facts.host, facts.pools),
        *quorum(facts),
    ]
    if not facts.standalone:
        findings += ha(facts.host, facts.ha)
    if facts.exporter != "active":
        findings.append(stop(facts.host, f"prometheus-node-exporter is {facts.exporter or '?'}"))
    return findings


def swarm_self(facts: VmFacts) -> list[Finding]:
    me = [node for node in facts.swarm if node.is_self]
    if me and not healthy_manager(me[0]):
        return [stop(facts.host, f"not back in the swarm ({me[0].status}, {me[0].manager})")]
    return []


def vm_recovered(facts: VmFacts) -> list[Finding]:
    findings = new_kernel(facts.host, facts.uname, facts.kernel)
    if facts.failed_units:
        findings.append(stop(facts.host, f"failed unit(s): {', '.join(facts.failed_units)}"))
    if facts.containers:
        findings.append(stop(facts.host, f"container(s) not up: {', '.join(facts.containers)}"))
    return findings + swarm_self(facts)
