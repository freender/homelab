"""Read a guest's live state from Proxmox, read-only, over SSH to a PVE node.

Everything goes through `pvesh ... --output-format json`, which any cluster node
answers for any guest (it proxies to the owning node), so one reachable node is
enough. The standalone node is not in the cluster's view and is asked directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...module_support import connection_for_host
from .drift import REFUSED, format_diff, guest_drift
from .pveconf import LiveHa, LiveReplication, ha_sid, parse_ha, parse_pending, parse_replication
from .spec import GuestSpec

# (node, command) -> stdout
Runner = Callable[[str, str], str]

RESOURCES = "pvesh get /cluster/resources --type vm --output-format json"
HA_RESOURCES = "pvesh get /cluster/ha/resources --output-format json"
HA_RULES = "pvesh get /cluster/ha/rules --output-format json"
REPLICATION = "pvesh get /cluster/replication --output-format json"


class NodeUnreachable(RuntimeError):
    pass


def pending_command(node: str, kind: str, vmid: int) -> str:
    # The pending endpoint returns the current value and any queued change in one
    # call, so drift (current) and pending are reported from the same read.
    return f"pvesh get /nodes/{node}/{kind}/{vmid}/pending --output-format json"


def ssh_runner(root: Path) -> Runner:
    connections: dict = {}

    def run(node: str, command: str) -> str:
        if node not in connections:
            connections[node] = connection_for_host(root, node)
        try:
            result = connections[node].connection.run(command, hide=True, warn=True)
        except Exception as exc:  # paramiko/socket/DNS: any of them means "try another node"
            raise NodeUnreachable(f"{node}: {exc}") from exc
        if result.exited != 0:
            raise NodeUnreachable(f"{node}: `{command}` exited {result.exited}")
        return result.stdout

    return run


@dataclass(frozen=True)
class ClusterView:
    node: str
    resources: dict[int, dict]
    ha: dict[str, LiveHa]
    replication: dict[int, list[LiveReplication]]


def query_nodes(spec: GuestSpec, nodes: dict[str, bool]) -> list[str]:
    """Nodes to ask, in order: the home node first, then the rest of its cluster.

    HA exists so that the home node can be down; the report must still work then.
    """
    if nodes[spec.home_node]:
        return [spec.home_node]
    others = [node for node, standalone in nodes.items() if not standalone]
    return [spec.home_node, *(node for node in others if node != spec.home_node)]


def view_key(spec: GuestSpec, nodes: dict[str, bool]) -> str:
    return spec.home_node if nodes[spec.home_node] else "cluster"


def read_cluster(runner: Runner, candidates: list[str]) -> ClusterView:
    errors = []
    for node in candidates:
        try:
            return _read_cluster_from(runner, node)
        except NodeUnreachable as exc:
            errors.append(str(exc))
    raise NodeUnreachable(f"no PVE node answered ({'; '.join(errors)})")


def _read_cluster_from(runner: Runner, node: str) -> ClusterView:
    resources = json.loads(runner(node, RESOURCES))
    return ClusterView(
        node=node,
        resources={int(item["vmid"]): item for item in resources},
        ha=parse_ha(json.loads(runner(node, HA_RESOURCES)), json.loads(runner(node, HA_RULES))),
        replication=parse_replication(json.loads(runner(node, REPLICATION))),
    )


def guest_report(spec: GuestSpec, view: ClusterView, runner: Runner) -> list[str]:
    resource = view.resources.get(spec.vmid)
    if resource is None:
        return [
            f"MISSING: no guest {spec.vmid} on {view.node}'s cluster; it would be created "
            f"on {spec.home_node} by `./guest create {spec.host}`"
        ]
    if resource.get("type") != spec.kind:
        return [f"{REFUSED}: vmid {spec.vmid} is {resource.get('type')}, the spec says {spec.kind}"]
    node = str(resource["node"])
    entries = json.loads(runner(view.node, pending_command(node, spec.kind, spec.vmid)))
    live, pending = parse_pending(entries)
    rows = guest_drift(
        spec,
        live,
        view.ha.get(ha_sid(spec.kind, spec.vmid)),
        view.replication.get(spec.vmid, []),
    )
    header = f"{spec.kind} {spec.vmid} on {node}, {resource.get('status', 'status unknown')}"
    if node != spec.home_node:
        header += f" (home node {spec.home_node})"
    lines = [f"{header}: {f'{len(rows)} diff(s)' if rows else 'in sync'}"]
    for row in rows:
        lines.append(f"  {format_diff(row)}")
        if row.fix:
            lines.append(f"    $ {row.fix}")
    lines += [
        f"  pending {change.key}: {change.current or '-'} -> "
        f"{'(delete)' if change.delete else change.pending}"
        for change in pending
    ]
    return lines
