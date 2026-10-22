"""What one host told the survey, as typed facts. Pure: sections in, facts out."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .parse import (
    AptChanges,
    Boot,
    HaStatus,
    Kernel,
    Quorum,
    ReplJob,
    SwarmNode,
    apt_changes,
    colon_fields,
    down_services,
    json_list,
    onboot_guests,
    parse_boot,
    parse_ha,
    parse_kernel,
    parse_quorum,
    parse_replication,
    parse_swarm,
    properties,
    sync_time,
    task_names,
    to_float,
    to_int,
    unhealthy_containers,
    unit_names,
)


def age(stamp: str, now: str) -> float | None:
    """Seconds between an mtime and the host's own clock, both epoch seconds."""
    then, current = to_float(stamp), to_float(now)
    if then is None or current is None:
        return None
    return current - then


@dataclass(frozen=True)
class Unreachable:
    host: str
    error: str


@dataclass(frozen=True)
class NodeFacts:
    host: str
    standalone: bool
    kernel: Kernel
    # Seconds dpkg last changed after reboot.prom was written: > 0 is a stale reading.
    kernel_lag: float | None
    uname: str
    uptime: float | None
    boot: Boot
    pools: str
    quorum: Quorum
    ha: HaStatus
    resources: list[dict] | None
    replication: list[ReplJob]
    clock: datetime | None
    ups: str | None
    tasks: list[str] | None
    upgrade: dict[str, str]
    apt: AptChanges | None
    lists_age: float | None
    exporter: str
    failed_units: list[str]
    down_units: list[str] | None
    onboot: list[int] | None


def node_facts(host: str, standalone: bool, found: dict[str, str]) -> NodeFacts:
    def get(name: str) -> str:
        return found.get(name, "")

    return NodeFacts(
        host=host,
        standalone=standalone,
        kernel=parse_kernel(get("kernel")),
        kernel_lag=age(get("kernelstamp"), get("dpkgstamp")),
        uname=get("uname"),
        uptime=to_float(get("uptime")),
        boot=parse_boot(get("boot")),
        pools=get("pools"),
        quorum=parse_quorum(get("quorum")),
        ha=parse_ha(get("ha")),
        resources=json_list(get("resources")),
        replication=parse_replication(get("replication")),
        clock=sync_time(get("clock")),
        ups=colon_fields(get("ups")).get("STATUS"),
        tasks=task_names(get("tasks")),
        upgrade=properties(get("upgrade")),
        apt=apt_changes(get("apt")),
        lists_age=age(get("lists"), get("epoch")),
        exporter=get("exporter"),
        failed_units=unit_names(get("failed")),
        down_units=down_services(get("enabled")),
        onboot=onboot_guests(get("onboot")),
    )


@dataclass(frozen=True)
class VmFacts:
    host: str
    kernel: Kernel
    kernel_lag: float | None
    uname: str
    uptime: float | None
    swarm: list[SwarmNode]
    addresses: str
    ci_jobs: int | None
    failed_units: list[str]
    upgrade: dict[str, str]
    apt: AptChanges | None
    lists_age: float | None
    containers: list[str] = field(default_factory=list)
    down_units: list[str] | None = None
    # The ssh user's manager state, read only where that user lingers: without
    # linger its units are session-bound and do not come back with the host.
    user_manager: str | None = None


def vm_facts(host: str, found: dict[str, str]) -> VmFacts:
    def get(name: str) -> str:
        return found.get(name, "")

    return VmFacts(
        host=host,
        kernel=parse_kernel(get("kernel")),
        kernel_lag=age(get("kernelstamp"), get("dpkgstamp")),
        uname=get("uname"),
        uptime=to_float(get("uptime")),
        swarm=parse_swarm(get("swarm")),
        addresses=get("addresses"),
        ci_jobs=to_int(get("ci")),
        failed_units=unit_names(get("failed")) + unit_names(get("userfailed")),
        upgrade=properties(get("upgrade")),
        apt=apt_changes(get("apt")),
        lists_age=age(get("lists"), get("epoch")),
        containers=unhealthy_containers(get("containers")),
        down_units=services_down(found, down_services(get("enabled"))),
        user_manager=get("usermanager") if lingers(found) else None,
    )


def lingers(found: dict[str, str]) -> bool:
    return found.get("linger", "") == "yes"


def services_down(found: dict[str, str], system: list[str] | None) -> list[str] | None:
    """System services that are down, plus the lingering user's. A user with no
    enabled units is not a failed reading, unlike a system manager with none."""
    if system is None or not lingers(found):
        return system
    return system + (down_services(found.get("userenabled", "")) or [])
