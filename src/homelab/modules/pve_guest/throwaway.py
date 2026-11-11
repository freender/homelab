"""`homelab guest throwaway`: short-lived guests that hosts.conf never names.

A throwaway is the same build as `./guest create` (create.py, build.py), from a
spec made up on the command line instead of read from hosts.conf: a bare Debian 13
guest that takes root's infra key, on DHCP, tagged `throwaway`, with the first
free vmid in THROWAWAY_VMIDS and a random Proxmox-prefix MAC. Nothing is
converged inside it: `./deploy` needs a hosts.conf entry, and a guest that needs
the fleet baseline is not a throwaway.

It has one disk, the os disk, and no HA, replication or protection, so destroying
it never takes data PBS does not hold. The nightly backup job still backs it up;
that is accepted. `destroy` re-checks all of this against the live guest, not the
flags it was made with.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape

from ...hosts import HostRegistry, default_registry
from ...op_secrets import offline_mode
from ...output import print_action, print_ok, print_sub, print_warn
from .create import Clock, CreateError, Steps, Target, build_target, infra_key, sites
from .inventory import load_guests, pve_nodes
from .live import ClusterView, NodeUnreachable, Runner, read_cluster, ssh_runner
from .pveconf import ha_sid
from .spec import PROFILES, THROWAWAY_TAG, THROWAWAY_VMIDS, GuestSpec, parse_guest

DEFAULT_PROFILES = {"lxc": "plain-ct", "qemu": "linux-vm"}
OS_DISK = {"lxc": "rootfs", "qemu": "scsi0"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
MAC_PREFIX = "BC:24:11"
ADDRESS_TIMEOUT = 120
POLL_SECONDS = 5


@dataclass(frozen=True)
class Request:
    kind: str
    node: str
    cores: int
    memory: int
    disk_gib: int
    vlan: int | None = None
    name: str | None = None
    profile: str | None = None


# --------------------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------------------


def random_mac(randbytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return ":".join([MAC_PREFIX, *(f"{byte:02X}" for byte in randbytes(3))])


def block(request: Request, vmid: int, mac: str) -> dict:
    """The `pve-guest` block a hosts.conf entry for this throwaway would hold."""
    net: dict = {"name": "nic0", "mac": mac}
    if request.vlan is not None:
        net["tag"] = request.vlan
    return {
        "vmid": vmid,
        "kind": request.kind,
        "home_node": request.node,
        "profile": request.profile or DEFAULT_PROFILES[request.kind],
        "cores": request.cores,
        "memory": request.memory,
        "onboot": False,
        "disks": {OS_DISK[request.kind]: {"role": "os", "size": f"{request.disk_gib}G"}},
        "net": {"net0": net},
        "extra": {"tags": THROWAWAY_TAG},
    }


def free_vmid(used: set[int]) -> int:
    for vmid in THROWAWAY_VMIDS:
        if vmid not in used:
            return vmid
    raise CreateError(
        f"no free throwaway vmid in {THROWAWAY_VMIDS.start}-{THROWAWAY_VMIDS.stop - 1}"
    )


def used_vmids(runner: Runner, nodes: dict[str, bool], home: str) -> set[int]:
    """Every vmid on the cluster and on each standalone node."""
    used: set[int] = set()
    for candidates in sites(nodes, home):
        used |= set(read_cluster(runner, candidates).resources)
    return used


def guest_name(registry: HostRegistry, requested: str | None, vmid: int) -> str:
    name = requested or f"tmp-{vmid}"
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"throwaway name {name!r} must be a lower-case DNS label")
    if name in registry.list_hosts():
        raise ValueError(f"{name} is a hosts.conf host; a throwaway needs its own name")
    return name


def node_domain(registry: HostRegistry, node: str) -> str:
    """The home node's own domain, which the throwaway's fqdn shares."""
    _, _, domain = str(registry.get(node, "config.hostname", "")).partition(".")
    if not domain:
        raise ValueError(f"{node}: config.hostname has no domain to name a throwaway in")
    return domain


def plan(
    registry: HostRegistry,
    request: Request,
    runner: Runner,
    randbytes: Callable[[int], bytes] = secrets.token_bytes,
) -> Target:
    nodes = pve_nodes(registry)
    if request.node not in nodes:
        raise ValueError(f"{request.node} is not a PVE node")
    vmid = free_vmid(used_vmids(runner, nodes, request.node))
    name = guest_name(registry, request.name, vmid)
    spec = parse_guest(name, block(request, vmid, random_mac(randbytes)), nodes)
    if not PROFILES[spec.profile].creatable:
        raise ValueError(f"profile {spec.profile} is not built by guest create")
    fqdn = f"{name}.{node_domain(registry, request.node)}"
    return Target(spec, fqdn, infra_key(registry), nodes)


def create_throwaway(
    root: Path,
    request: Request,
    dry_run: bool,
    runner: Runner | None = None,
    clock: Clock | None = None,
    randbytes: Callable[[int], bytes] = secrets.token_bytes,
) -> None:
    if offline_mode():
        raise ValueError("guest throwaway needs the PVE nodes; unset offline validation")
    runner = runner or ssh_runner(root)
    clock = clock or Clock()
    target = plan(default_registry(root), request, runner, randbytes)
    if build_target(target, dry_run, runner, clock):
        report_address(target.spec, wait_for_address(runner, target.spec, clock))


def interfaces_command(spec: GuestSpec) -> str:
    base = f"pvesh get /nodes/{spec.home_node}/{spec.kind}/{spec.vmid}"
    path = "interfaces" if spec.kind == "lxc" else "agent/network-get-interfaces"
    return f"{base}/{path} --output-format json"


def ipv4_for(payload: object, mac: str) -> str | None:
    """The first IPv4 on the NIC with `mac`, from either PVE interfaces shape."""
    entries = payload.get("result", []) if isinstance(payload, dict) else payload
    for entry in entries or []:
        if str(entry.get("hardware-address", "")).lower() != mac.lower():
            continue
        for address in entry.get("ip-addresses") or []:
            if address.get("ip-address-type") in ("inet", "ipv4"):
                return str(address["ip-address"])
    return None


def wait_for_address(runner: Runner, spec: GuestSpec, clock: Clock) -> str | None:
    """nic0's DHCP address, or None if it has none by ADDRESS_TIMEOUT."""
    command = interfaces_command(spec)
    mac = spec.nets[0].mac
    deadline = clock.now() + ADDRESS_TIMEOUT
    while True:
        try:
            address = ipv4_for(json.loads(runner(spec.home_node, command)), mac)
        except NodeUnreachable:
            address = None
        if address or clock.now() >= deadline:
            return address
        clock.sleep(POLL_SECONDS)


def report_address(spec: GuestSpec, address: str | None) -> None:
    where = f"{spec.kind} {spec.vmid} ({spec.host}) on {spec.home_node}"
    if address is None:
        print_warn(f"{where} is up but has no DHCP address after {ADDRESS_TIMEOUT}s")
    else:
        print_ok(f"{where}: ssh root@{address}")
    print_sub(f"remove it with: ./guest throwaway destroy {spec.vmid}")


# --------------------------------------------------------------------------------------
# list and destroy
# --------------------------------------------------------------------------------------


def is_throwaway(resource: dict) -> bool:
    return THROWAWAY_TAG in re.split(r"[;, ]+", str(resource.get("tags") or ""))


def views(runner: Runner, nodes: dict[str, bool]) -> list[ClusterView]:
    """One view of the cluster and one of each standalone node."""
    return [read_cluster(runner, candidates) for candidates in sites(nodes, "")]


def list_throwaways(root: Path, runner: Runner | None = None) -> None:
    runner = runner or ssh_runner(root)
    found = [
        resource
        for view in views(runner, pve_nodes(default_registry(root)))
        for resource in view.resources.values()
        if is_throwaway(resource)
    ]
    if not found:
        print_ok("no throwaway guests")
        return
    for resource in sorted(found, key=lambda item: int(item["vmid"])):
        print_sub(
            escape(
                f"{resource['vmid']}  {resource.get('type')}  {resource.get('name')}  "
                f"on {resource.get('node')}  {resource.get('status')}"
            )
        )


def find_guest(runner: Runner, nodes: dict[str, bool], vmid: int) -> tuple[ClusterView, dict]:
    for view in views(runner, nodes):
        if vmid in view.resources:
            return view, view.resources[vmid]
    raise CreateError(f"no guest {vmid} on any PVE node")


def refusal(vmid: int, resource: dict, view: ClusterView, declared: dict[int, str]) -> str | None:
    """Why destroying this guest would be wrong, or None for a throwaway."""
    kind = str(resource.get("type"))
    reasons = {
        f"it is {declared.get(vmid)} in hosts.conf": vmid in declared,
        f"it is outside the throwaway range {THROWAWAY_VMIDS.start}-"
        f"{THROWAWAY_VMIDS.stop - 1}": vmid not in THROWAWAY_VMIDS,
        f"it has no {THROWAWAY_TAG} tag": not is_throwaway(resource),
        "it is HA-managed": ha_sid(kind, vmid) in view.ha,
        "it is replicated": bool(view.replication.get(vmid)),
    }
    found = [reason for reason, applies in reasons.items() if applies]
    return "; ".join(found) or None


def destroy_throwaway(root: Path, vmid: int, dry_run: bool, runner: Runner | None = None) -> None:
    if offline_mode():
        raise ValueError("guest throwaway needs the PVE nodes; unset offline validation")
    registry = default_registry(root)
    declared = {spec.vmid: host for host, spec in load_guests(registry).items()}
    runner = runner or ssh_runner(root)
    view, resource = find_guest(runner, pve_nodes(registry), vmid)
    reason = refusal(vmid, resource, view, declared)
    if reason:
        raise CreateError(f"refusing to destroy {vmid}: {reason}")
    node, kind = str(resource["node"]), str(resource["type"])
    config = json.loads(
        runner(view.node, f"pvesh get /nodes/{node}/{kind}/{vmid}/config --output-format json")
    )
    if str(config.get("protection", "0")) == "1":
        raise CreateError(f"refusing to destroy {vmid}: protection is on")
    tool = "qm" if kind == "qemu" else "pct"
    print_action(
        f"{'Dry run: ' if dry_run else ''}destroy throwaway {kind} {vmid} "
        f"({resource.get('name')}) on {node}"
    )
    steps = Steps(runner, node, dry_run, Clock())
    if resource.get("status") == "running":
        steps.run("stop it", f"{tool} stop {vmid}")
    steps.run("destroy it", f"{tool} destroy {vmid} --purge 1 --destroy-unreferenced-disks 1")
    if not dry_run:
        print_ok(f"{kind} {vmid} destroyed")
