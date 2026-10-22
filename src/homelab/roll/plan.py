"""Turn the survey into the roll: waves of nodes, then VMs. Pure.

Wave policy (the `/roll` command owns the why):

- One cluster node per wave, ever. Standalone nodes hold no corosync vote, so
  they ride along with the first wave.
- The node currently running the controller (riven: this OpenCode session and
  the shared SSH agent) goes last, so every other wave is verified before the
  one reboot that live-migrates the session.
- The node currently running tower goes after the other remaining node, so it
  reboots alone.
- A node with nothing pending drops out; an emptied wave disappears.
- VMs follow every node wave, the controller last of all.
"""

from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass, field

from ..modules.apt_upgrade import SERVICE_NAME as UPGRADE_SERVICE
from . import checks
from .checks import NOTE, STOP, WARN, Finding
from .facts import NodeFacts, Unreachable, VmFacts

# Held to hosts.conf by `./validate` (check_roll_inventory): a rename fails CI
# rather than quietly dropping out of the wave order.
TOWER = "tower"
CONTROLLER = "riven"


@dataclass(frozen=True)
class Inventory:
    # PVE node -> standalone, in hosts.conf order.
    nodes: dict[str, bool]
    # Guests that boot their own kernel, in hosts.conf order.
    vms: list[str]
    # Every pve-guest host -> vmid.
    vmids: dict[str, int]
    pbs: list[str] = field(default_factory=list)
    vips: dict[str, list[str]] = field(default_factory=dict)
    # Own-kernel VM -> the login user whose `systemctl --user` units must come back.
    users: dict[str, str] = field(default_factory=dict)
    monitor: str | None = None
    # Hosts the on-demand pve-upgrade module can dist-upgrade.
    upgraders: list[str] = field(default_factory=list)
    # Node or own-kernel VM -> `user@hostname`, the root login the survey uses.
    targets: dict[str, str] = field(default_factory=dict)


def reboot_command(inventory: Inventory, host: str) -> str:
    """What the operator runs to reboot `host`; `./roll` never runs it.

    The controller's reboot is delayed and detached, so the session that issues
    it can finish its reply before the reboot ends it.
    """
    target = inventory.targets.get(host)
    if not target:
        return ""
    remote = "systemctl reboot"
    if host == CONTROLLER:
        remote = f"systemd-run --on-active=1min --unit={CONTROLLER}-reboot {remote}"
    return f"ssh {shlex.quote(target)} {shlex.quote(remote)}"


def remedy(inventory: Inventory, host: str) -> str:
    """How to install a pending kernel now, before the roll rather than during it."""
    if host in inventory.upgraders:
        return f"./deploy --confirm-upgrade pve-upgrade {host}"
    return f"systemctl start {UPGRADE_SERVICE} on {host}, as root"


def scoped(inventory: Inventory, requested: list[str]) -> list[str]:
    """The requested hosts in roll order; everything when nothing is requested."""
    known = [*inventory.nodes, *inventory.vms]
    unknown = sorted(set(requested) - set(known))
    if unknown:
        raise ValueError(
            f"not a PVE node or an own-kernel VM: {', '.join(unknown)} "
            f"(choose from {', '.join(known)})"
        )
    return [host for host in known if not requested or host in requested]


def to_end(nodes: list[str], node: str | None) -> list[str]:
    """Move `node` to the end, if present."""
    return [n for n in nodes if n != node] + [n for n in nodes if n == node]


def order_waves(
    cluster: list[str], standalone: list[str], tower: str | None, controller: str | None
) -> list[list[str]]:
    last = [controller] if controller in cluster else []
    rest = to_end([node for node in cluster if node not in last], tower)
    waves = [[*standalone, *rest[:1]], *([node] for node in rest[1:]), last]
    return [wave for wave in waves if wave]


def keep(waves: list[list[str]], wanted: set[str]) -> list[list[str]]:
    kept = [[node for node in wave if node in wanted] for wave in waves]
    return [wave for wave in kept if wave]


def order_vms(vms: list[str], controller: str) -> list[str]:
    return [vm for vm in vms if vm != controller] + [vm for vm in vms if vm == controller]


@dataclass(frozen=True)
class Step:
    host: str
    running: str
    installed: str
    details: list[str]
    reboot: str = ""


@dataclass
class Plan:
    waves: list[list[Step]] = field(default_factory=list)
    vms: list[Step] = field(default_factory=list)
    stops: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    held: list[Finding] = field(default_factory=list)
    current: list[str] = field(default_factory=list)
    not_built: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.stops:
            return "stop"
        if self.waves or self.vms:
            return "ready"
        return "nothing-pending"

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, **asdict(self)}


def sid(resource: dict) -> str:
    return f"{'vm' if resource.get('type') == 'qemu' else 'ct'}:{resource.get('vmid')}"


def where(resources: list[dict], vmid: int | None) -> str | None:
    for resource in resources:
        if vmid is not None and resource.get("vmid") == vmid:
            return str(resource.get("node"))
    return None


def guest_line(resource: dict, ha_sids: set[str]) -> str:
    name = f"{resource.get('name')} ({sid(resource)}"
    if sid(resource) not in ha_sids:
        return f"{name}, no HA: down with the node until it boots)"
    if resource.get("type") == "qemu":
        return f"{name}, HA: live-migrates)"
    return f"{name}, HA: LXC restarts on another node)"


def guests_on(node: str, resources: list[dict], ha_sids: set[str]) -> list[str]:
    return [
        guest_line(resource, ha_sids)
        for resource in sorted(resources, key=lambda item: int(item.get("vmid", 0)))
        if resource.get("node") == node and resource.get("status") == "running"
    ]


def demote(findings: list[Finding]) -> list[Finding]:
    """Stops on a host that is not being rebooted only warn."""
    return [
        Finding(WARN, item.host, f"{item.message} (not pending)") if item.level == STOP else item
        for item in findings
    ]


@dataclass(frozen=True)
class Survey:
    nodes: dict[str, NodeFacts | Unreachable]
    vms: dict[str, VmFacts | Unreachable]
    pbs_tasks: dict[str, list[str] | None] = field(default_factory=dict)


def cluster_view(survey: Survey) -> NodeFacts | None:
    for facts in survey.nodes.values():
        if isinstance(facts, NodeFacts) and not facts.standalone:
            return facts
    return None


def known_resources(survey: Survey, view: NodeFacts | None) -> list[dict] | None:
    """Every guest PVE knows of. None when the cluster could not be read.

    `/cluster/resources` on a cluster node does not list a standalone node's guests
    (xur and deepstone on osiris), so each standalone node's own answer is merged in.
    """
    if view is None or view.resources is None:
        return None
    merged = list(view.resources)
    for facts in survey.nodes.values():
        if isinstance(facts, NodeFacts) and facts.standalone and facts.resources:
            merged += [item for item in facts.resources if item.get("node") == facts.host]
    return merged


def pbs_findings(node: str, survey: Survey, resources: list[dict]) -> list[Finding]:
    """A PBS guest goes down with its node: its running jobs block that node."""
    findings: list[Finding] = []
    for pbs, running in survey.pbs_tasks.items():
        if any(r.get("name") == pbs and r.get("node") == node for r in resources):
            findings += checks.tasks(node, running, owner=pbs)
    return findings


def sort_findings(plan: Plan, findings: list[Finding], pending: bool) -> None:
    for item in findings if pending else demote(findings):
        target = plan.stops if item.level == STOP else plan.warnings
        target.append(item)


def plan_nodes(
    plan: Plan, inventory: Inventory, survey: Survey, scope: list[str], resources: list[dict]
) -> set[str]:
    pending: set[str] = set()
    for host in (node for node in inventory.nodes if node in scope):
        facts = survey.nodes.get(host)
        if not isinstance(facts, NodeFacts):
            plan.stops.append(unreachable(host, facts))
            continue
        due = checks.pending(facts.kernel, facts.kernel_lag, facts.uname)
        findings = checks.node_preflight(facts, remedy(inventory, host))
        findings += pbs_findings(host, survey, resources)
        sort_findings(plan, findings, due is not False)
        if due:
            pending.add(host)
        elif due is False:
            plan.current.append(host)
    return pending


def node_step(
    inventory: Inventory, facts: NodeFacts, resources: list[dict], ha_sids: set[str]
) -> Step:
    return Step(
        facts.host,
        facts.uname,
        facts.kernel.installed,
        guests_on(facts.host, resources, ha_sids),
        reboot_command(inventory, facts.host),
    )


def build_waves(
    plan: Plan,
    inventory: Inventory,
    survey: Survey,
    pending: set[str],
    view: NodeFacts | None,
    resources: list[dict],
) -> None:
    cluster = [node for node, alone in inventory.nodes.items() if not alone]
    standalone = [node for node, alone in inventory.nodes.items() if alone]
    order = order_waves(
        cluster,
        standalone,
        where(resources, inventory.vmids.get(TOWER)),
        where(resources, inventory.vmids.get(CONTROLLER)),
    )
    ha_sids = set(view.ha.services) if view else set()
    surveyed = {host: f for host, f in survey.nodes.items() if isinstance(f, NodeFacts)}
    plan.waves = [
        [node_step(inventory, surveyed[node], resources, ha_sids) for node in wave]
        for wave in keep(order, pending)
    ]


def vm_exists(inventory: Inventory, host: str, resources: list[dict] | None) -> bool:
    if resources is None:
        return True
    return any(r.get("vmid") == inventory.vmids.get(host) for r in resources)


def unreachable(host: str, facts: object) -> Finding:
    error = facts.error if isinstance(facts, Unreachable) else "not surveyed"
    return checks.stop(host, f"unreachable: {error}")


def by_level(findings: list[Finding], *levels: str) -> list[Finding]:
    return [item for item in findings if item.level in levels]


def plan_vm(
    plan: Plan, inventory: Inventory, host: str, facts: VmFacts | Unreachable | None
) -> None:
    if not isinstance(facts, VmFacts):
        plan.held.append(unreachable(host, facts))
        return
    findings = checks.vm_preflight(facts, inventory.vips.get(host, []), remedy(inventory, host))
    if checks.pending(facts.kernel, facts.kernel_lag, facts.uname) is False:
        plan.current.append(host)
        plan.warnings += demote(by_level(findings, STOP, WARN))
    elif by_level(findings, STOP):
        plan.held += by_level(findings, STOP)
    else:
        plan.warnings += by_level(findings, WARN)
        notes = [item.message for item in by_level(findings, NOTE)]
        plan.vms.append(
            Step(host, facts.uname, facts.kernel.installed, notes, reboot_command(inventory, host))
        )


def plan_vms(
    plan: Plan, inventory: Inventory, survey: Survey, scope: list[str], resources: list[dict] | None
) -> None:
    for host in order_vms([vm for vm in inventory.vms if vm in scope], CONTROLLER):
        if not vm_exists(inventory, host, resources):
            plan.not_built.append(host)
            continue
        plan_vm(plan, inventory, host, survey.vms.get(host))


def build_plan(
    inventory: Inventory, survey: Survey, scope: list[str], window: list[Finding]
) -> Plan:
    plan = Plan()
    view = cluster_view(survey)
    if view is None:
        plan.stops.append(checks.stop("cluster", "no cluster node answered"))
    known = known_resources(survey, view)
    resources = known or []
    pending = plan_nodes(plan, inventory, survey, scope, resources)
    build_waves(plan, inventory, survey, pending, view, resources)
    plan_vms(plan, inventory, survey, scope, known)
    if plan.waves or plan.vms:
        if view is not None:
            sort_findings(plan, checks.ha("cluster", view.ha), True)
        sort_findings(plan, window, True)
    return plan


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def step_lines(step: Step) -> list[str]:
    lines = [f"  {step.host:<8} {step.running or '?'} -> {step.installed or '?'}"]
    lines += [f"      {detail}" for detail in step.details]
    return lines + ([f"      reboot: {step.reboot}"] if step.reboot else [])


def finding_lines(title: str, findings: list[Finding]) -> list[str]:
    if not findings:
        return []
    return ["", title, *(f"  {item.host}: {item.message}" for item in findings)]


def render(plan: Plan) -> list[str]:
    lines = [f"verdict: {plan.verdict}"]
    if plan.stops:
        lines.append("Do not reboot anything: clear the stop conditions, then plan again.")
    for number, wave in enumerate(plan.waves, start=1):
        lines += ["", f"Wave {number}: {', '.join(step.host for step in wave)}"]
        for step in wave:
            lines += step_lines(step)
    if plan.vms:
        lines += ["", "VMs, after every node wave (reboot from inside the guest):"]
        for step in plan.vms:
            lines += step_lines(step)
    lines += finding_lines("Held: still pending, kept out of this roll", plan.held)
    lines += finding_lines("Stop conditions", plan.stops)
    lines += finding_lines("Warnings", plan.warnings)
    if plan.current:
        lines += ["", f"Nothing pending: {', '.join(plan.current)}"]
    if plan.not_built:
        lines += [f"Not built yet: {', '.join(plan.not_built)}"]
    return lines
