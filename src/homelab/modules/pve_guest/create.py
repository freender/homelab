"""`homelab guest create <host>`: build a new guest from its `pve-guest` spec, once.

This is not an apply path: it refuses when the vmid exists on any PVE node, and
never touches a running guest. It stops once the guest boots and takes root's
key. HA and replication are left for the drift report to list, and the rest is
`./deploy all <host>` (map homelab-ops#148, ticket #171).

A VM is created on its home node from a fresh, checksum-checked cloud image.
The guest agent's first answer proves cloud-init ran. cloud-init is then
switched off and its drive removed, and a second boot proves the guest comes up
without it. Files left on the node (image, snippets, key) are removed however
the run ends. A guest that failed half-way is kept for inspection.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape

from ...hosts import HostRegistry, default_registry
from ...op_secrets import offline_mode
from ...output import print_action, print_ok, print_sub, print_warn
from . import build
from .inventory import pve_nodes
from .live import NodeUnreachable, Runner, guest_report, query_nodes, read_cluster, ssh_runner
from .spec import FEATURE, GuestSpec, parse_guest

AGENT_TIMEOUT = 600
CLOUD_INIT_TIMEOUT = 900
SHUTDOWN_TIMEOUT = 180
POLL_SECONDS = 5
# `cloud-init status --wait`: 0 done, 2 done with recoverable errors (warnings).
CLOUD_INIT_OK = (0, 2)


class CreateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    spec: GuestSpec
    fqdn: str
    root_key: str
    nodes: dict[str, bool]


@dataclass
class Clock:
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic


def infra_key(registry: HostRegistry) -> str:
    """Root's key for new guests: the one PVE installs also get (pve-autoinstall)."""
    keys = {
        host: registry.get(host, "pve-autoinstall.root_ssh_key", None)
        for host in registry.list_hosts()
    }
    found = sorted((host, key) for host, key in keys.items() if key)
    if len(found) != 1:
        hosts = ", ".join(host for host, _ in found) or "none"
        raise ValueError(f"need exactly one pve-autoinstall.root_ssh_key, found: {hosts}")
    return str(found[0][1])


def load_target(registry: HostRegistry, host: str) -> Target:
    nodes = pve_nodes(registry)
    raw = registry.get(host, FEATURE, None)
    if raw is None:
        raise ValueError(f"{host} has no {FEATURE} block")
    spec = parse_guest(host, raw, nodes)
    fqdn = registry.get(host, "config.hostname", None)
    if not fqdn or not str(fqdn).startswith(f"{host}."):
        raise ValueError(f"{host}: config.hostname must be the guest's fqdn ({host}.<domain>)")
    return Target(spec, str(fqdn), infra_key(registry), nodes)


def sites(nodes: dict[str, bool], home: str) -> list[list[str]]:
    """Each separate set of PVE nodes, as the nodes to try in order: a vmid must be
    free on the cluster and on every standalone node (they share PBS namespaces)."""
    cluster = [node for node, standalone in nodes.items() if not standalone]
    cluster.sort(key=lambda node: node != home)
    alone = [[node] for node, standalone in nodes.items() if standalone]
    return ([cluster] if cluster else []) + alone


def check_vmid_free(runner: Runner, target: Target) -> None:
    spec = target.spec
    for candidates in sites(target.nodes, spec.home_node):
        view = read_cluster(runner, candidates)
        found = view.resources.get(spec.vmid)
        if found is not None:
            raise CreateError(
                f"vmid {spec.vmid} already exists on {found.get('node')} "
                f"({found.get('type')} {found.get('name')}); create never touches a guest"
            )


class Steps:
    """Runs (or, in a dry run, prints) commands on the home node."""

    def __init__(self, runner: Runner, node: str, dry_run: bool, clock: Clock) -> None:
        self.runner = runner
        self.node = node
        self.dry_run = dry_run
        self.clock = clock

    def read(self, cmd: str) -> str:
        return self.runner(self.node, cmd)

    def run(self, what: str, cmd: str, show: bool = True) -> str:
        print_sub(escape(f"{'would ' if self.dry_run else ''}{what}"))
        if self.dry_run:
            if show:
                print_sub(escape(f"  $ {cmd}"))
            return ""
        return self.runner(self.node, cmd)

    def wait_for_agent(self, vmid: int) -> None:
        print_sub(f"{'would wait' if self.dry_run else 'waiting'} for the guest agent on {vmid}")
        if self.dry_run:
            return
        deadline = self.clock.now() + AGENT_TIMEOUT
        while True:
            try:
                self.runner(self.node, f"qm agent {vmid} ping")
                return
            except NodeUnreachable:
                if self.clock.now() >= deadline:
                    raise CreateError(
                        f"no guest agent on {vmid} after {AGENT_TIMEOUT}s; "
                        "check the console for cloud-init"
                    ) from None
                self.clock.sleep(POLL_SECONDS)

    def guest_exec(self, what: str, vmid: int, argv: list[str], timeout: int) -> int:
        cmd = build.command(
            ["qm", "guest", "exec", str(vmid), "--timeout", str(timeout), "--", *argv]
        )
        out = self.run(what, cmd)
        return int(json.loads(out).get("exitcode", -1)) if out else 0


def create_guest(
    root: Path, host: str, dry_run: bool, runner: Runner | None = None, clock: Clock | None = None
) -> None:
    if offline_mode():
        raise ValueError("guest create needs the PVE nodes; unset offline validation")
    target = load_target(default_registry(root), host)
    spec = target.spec
    runner = runner or ssh_runner(root)
    clock = clock or Clock()
    print_action(
        f"{'Dry run: ' if dry_run else ''}create {spec.kind} {spec.vmid} ({host}) "
        f"on {spec.home_node}"
    )
    check_vmid_free(runner, target)
    print_ok(f"vmid {spec.vmid} is free on every PVE node")
    steps = Steps(runner, spec.home_node, dry_run, clock)
    build_guest = create_vm if spec.kind == "qemu" else create_lxc
    if dry_run:
        build_guest(steps, target)
        return
    try:
        build_guest(steps, target)
    except Exception:
        print_warn(
            f"create stopped part-way; anything already built under vmid {spec.vmid} "
            f"on {spec.home_node} is kept for inspection"
        )
        raise
    finally:
        remove_scratch(runner, spec)
    report(runner, target)


def create_vm(steps: Steps, target: Target) -> None:
    spec = target.spec
    digest = build.image_checksum(steps.read(f"curl -fsSL {build.SUMS_URL}"))
    print_ok(f"{build.IMAGE_NAME} sha512 {digest[:16]}...")
    steps.run(f"download {build.IMAGE_NAME}", build.command(build.download(spec, digest)))
    user, network = build.snippet_files(spec.vmid)
    snippets = {
        user: build.user_data(spec, target.root_key, target.fqdn),
        network: build.network_config(spec),
    }
    for name, content in snippets.items():
        path = f"{build.LOCAL_DIR}/snippets/{name}"
        steps.run(f"write {path}", build.write_file(path, content), show=False)
        if steps.dry_run:
            for line in content.splitlines():
                print_sub(escape(f"  | {line}"))
    image = f"local:import/{build.image_file(spec.vmid)}"
    steps.run(f"create VM {spec.vmid}", build.command(build.vm_create(spec, image)))
    steps.run("grow the os disk", build.command(build.vm_resize(spec)))
    steps.run("start it", f"qm start {spec.vmid}")
    steps.wait_for_agent(spec.vmid)
    status = steps.guest_exec(
        "wait for cloud-init", spec.vmid, ["cloud-init", "status", "--wait"], CLOUD_INIT_TIMEOUT
    )
    if status not in CLOUD_INIT_OK:
        raise CreateError(f"cloud-init on {spec.vmid} ended with status {status}")
    steps.guest_exec(
        "switch cloud-init off",
        spec.vmid,
        ["touch", "/etc/cloud/cloud-init.disabled"],
        60,
    )
    steps.run("shut it down", f"qm shutdown {spec.vmid} --timeout {SHUTDOWN_TIMEOUT}")
    steps.run("remove the cloud-init drive", f"qm set {spec.vmid} --delete ide2,cicustom")
    steps.run("start it again", f"qm start {spec.vmid}")
    steps.wait_for_agent(spec.vmid)


def create_lxc(steps: Steps, target: Target) -> None:
    spec = target.spec
    steps.run("refresh the template index", "pveam update")
    template = build.newest_template(steps.read("pveam available --section system"))
    print_ok(f"template {template}")
    steps.run(f"download {template}", build.command(["pveam", "download", "local", template]))
    key = build.key_file(spec.vmid)
    content = target.root_key + "\n"
    steps.run(f"write root's key to {key}", build.write_file(key, content), show=False)
    volid = f"local:vztmpl/{template}"
    steps.run(f"create CT {spec.vmid}", build.command(build.lxc_create(spec, volid)))
    steps.run("start it", f"pct start {spec.vmid}")


def remove_scratch(runner: Runner, spec: GuestSpec) -> None:
    try:
        runner(spec.home_node, build.command(["rm", "-f", *build.scratch_paths(spec)]))
    except NodeUnreachable as exc:
        print_warn(f"could not remove {', '.join(build.scratch_paths(spec))}: {exc}")


def report(runner: Runner, target: Target) -> None:
    spec = target.spec
    view = read_cluster(runner, query_nodes(spec, target.nodes))
    for line in guest_report(spec, view, runner):
        print_sub(escape(line))
    print_ok(f"{spec.kind} {spec.vmid} created; next: ./deploy all {spec.host}")
    if spec.replication or spec.ha:
        print_sub("replication and HA are not created here; the report above lists them")
