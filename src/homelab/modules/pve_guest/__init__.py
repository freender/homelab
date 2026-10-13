"""pve-guest: each guest's Proxmox spec, validated, and a read-only drift report.

The spec is the `pve-guest` block on the guest's own hosts.conf entry (map
homelab-ops#148). `./validate` checks every block and every vmid written by hand
elsewhere in hosts.conf; `./deploy [--dry-run] pve-guest <guest>` compares the
block with the guest's current config and labels each difference by what applying
it would take. There is no apply path: a live run reports exactly what a dry run
does and changes nothing. A guest that does not exist yet is built once, from
the same block, by `./guest create <host>` (outside `./deploy`).

    pveconf    - parse/normalize Proxmox output (pure)
    spec       - schema, profiles, roles, per-guest validation (pure)
    inventory  - checks across guests and against hand-written vmids
    drift      - spec vs live, impact labels (pure)
    live       - pvesh reads over SSH, the per-guest report
    build      - create's command lines and cloud-init documents (pure)
    create     - `./guest create`: runs them on the home node
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape

from ...deploy import DeploySession
from ...hosts import default_registry
from ...module_support import run_module_deploy
from ...op_secrets import offline_mode
from ...output import print_sub
from .inventory import pve_nodes, validate_inventory
from .live import ClusterView, Runner, guest_report, query_nodes, read_cluster, ssh_runner, view_key
from .spec import FEATURE, parse_guest

__all__ = ["deploy", "validate"]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    # One cluster read per deploy run, shared by every guest in it.
    views: dict[str, ClusterView] = {}
    runner = ssh_runner(root)
    return run_module_deploy(
        root,
        requested_host,
        FEATURE,
        session,
        lambda host: deploy_host(root, host, dry_run, views, runner),
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    validate_inventory(default_registry(root))


def deploy_host(
    root: Path,
    host: str,
    dry_run: bool,
    views: dict[str, ClusterView],
    runner: Runner,
) -> None:
    registry = default_registry(root)
    nodes = pve_nodes(registry)
    spec = parse_guest(host, registry.get(host, FEATURE, None), nodes)
    if offline_mode():
        print_sub(f"[?] {spec.kind} {spec.vmid}: live drift skipped (offline validation)")
        return
    key = view_key(spec, nodes)
    if key not in views:
        views[key] = read_cluster(runner, query_nodes(spec, nodes))
    for line in guest_report(spec, views[key], runner):
        # Report lines carry live values; rich would eat anything shaped like [markup].
        print_sub(escape(line))
    if not dry_run:
        print_sub("report only: pve-guest has no apply path and changed nothing")
