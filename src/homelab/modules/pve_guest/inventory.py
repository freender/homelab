"""Checks across every `pve-guest` block, and against vmids written by hand elsewhere.

Before `pve-guest`, a vmid appeared in hosts.conf only where another module needed
one (zfs-automation's migratable LXC plans, pve-backup's restore list and job
filters), with nothing to say which guest it was. Each of those must now name a
declared guest of the right kind, so retiring or renumbering a guest cannot leave a
stale vmid behind.
"""

from __future__ import annotations

import re

from ...hosts import HostRegistry
from .spec import FEATURE, GuestSpec, parse_guest

SUBVOL_RE = re.compile(r"/(?:subvol|vm)-(\d+)-disk-\d+$")


def pve_nodes(registry: HostRegistry) -> dict[str, bool]:
    """Every PVE node in hosts.conf, mapped to whether it is standalone."""
    return {
        host: registry.get(host, "config.standalone", False) is True
        for host in registry.list_hosts()
        if registry.get(host, "config.type") == "pve"
    }


def declared_guests(registry: HostRegistry) -> list[str]:
    """Hosts with a `pve-guest` block, including one gated off with `deploy: false`.

    The gate only stops the drift report; the guest still exists and still owns its
    vmid, so it stays in every inventory check.
    """
    return [
        host
        for host, config in registry.load().items()
        if FEATURE in (config.get("features") or {})
    ]


def load_guests(registry: HostRegistry) -> dict[str, GuestSpec]:
    nodes = pve_nodes(registry)
    guests: dict[str, GuestSpec] = {}
    owners: dict[int, str] = {}
    for host in declared_guests(registry):
        if host in nodes:
            raise ValueError(f"{host}: {FEATURE} is for guests, not PVE nodes")
        spec = parse_guest(host, registry.get(host, FEATURE, None), nodes)
        if spec.vmid in owners:
            raise ValueError(f"{host}: vmid {spec.vmid} is already used by {owners[spec.vmid]}")
        owners[spec.vmid] = host
        guests[host] = spec
    return guests


def check_vmid_references(registry: HostRegistry, guests: dict[str, GuestSpec]) -> int:
    """Every hand-written vmid names a declared guest. Returns how many were checked."""
    by_vmid = {spec.vmid: spec for spec in guests.values()}
    checked = 0
    for host in registry.list_hosts():
        for where, vmid, kind, node in vmid_references(registry, host):
            _check_reference(by_vmid, where, vmid, kind, node)
            checked += 1
    return checked


def _check_reference(
    by_vmid: dict[int, GuestSpec], where: str, vmid: int, kind: str | None, node: str | None
) -> None:
    spec = by_vmid.get(vmid)
    if spec is None:
        raise ValueError(f"{where}: vmid {vmid} is not a declared {FEATURE} guest")
    if kind is not None and spec.kind != kind:
        raise ValueError(f"{where}: vmid {vmid} ({spec.host}) is {spec.kind}, expected {kind}")
    if node is not None and spec.home_node != node:
        raise ValueError(
            f"{where}: vmid {vmid} ({spec.host}) lives on {spec.home_node}, not {node}"
        )


def vmid_references(registry: HostRegistry, host: str):
    """(where, vmid, required kind, required home node) for each hand-written vmid."""
    yield from _migratable_lxc_references(registry, host)
    yield from _restore_references(registry, host)
    yield from _backup_job_references(registry, host)


def _migratable_lxc_references(registry: HostRegistry, host: str):
    groups = registry.get(host, "zfs-automation.migratable_lxc_groups", None) or {}
    for group, body in groups.items():
        for index, plan in enumerate((body or {}).get("plans") or []):
            where = f"{host}: zfs-automation.migratable_lxc_groups.{group}.plans[{index}]"
            vmid = _vmid(plan.get("vmid"), where)
            match = SUBVOL_RE.search(str(plan.get("dataset", "")))
            if match and int(match.group(1)) != vmid:
                raise ValueError(f"{where}: dataset {plan['dataset']} is not vmid {vmid}'s")
            yield where, vmid, plan.get("kind", "lxc"), None


def _restore_references(registry: HostRegistry, host: str):
    vmids = registry.get(host, "pve-backup.restore_lxc_configs.vmids", None) or []
    for index, vmid in enumerate(vmids):
        where = f"{host}: pve-backup.restore_lxc_configs.vmids[{index}]"
        yield where, _vmid(vmid, where), "lxc", host


def _backup_job_references(registry: HostRegistry, host: str):
    jobs = registry.get(host, "pve-backup.pbs_setup.jobs", None) or []
    for index, job in enumerate(jobs):
        for field in ("vmid", "exclude"):
            where = f"{host}: pve-backup.pbs_setup.jobs[{index}].{field}"
            for item in str(job.get(field) or "").split(","):
                if item.strip():
                    yield where, _vmid(item.strip(), where), None, host


def _vmid(value: object, where: str) -> int:
    try:
        vmid = int(str(value))
    except ValueError:
        raise ValueError(f"{where}: {value!r} is not a vmid") from None
    if vmid <= 0:
        raise ValueError(f"{where}: {value!r} is not a vmid")
    return vmid


def validate_inventory(registry: HostRegistry) -> tuple[int, int]:
    """Validate every guest block and every vmid reference. Returns the two counts."""
    guests = load_guests(registry)
    return len(guests), check_vmid_references(registry, guests)
