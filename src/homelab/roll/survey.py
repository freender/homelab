"""Read every host the roll touches, over SSH, read-only; then plan or verify.

One SSH command per host: a script that prints each reading under an `@@name`
marker, so an unreachable host costs one connect timeout, not one per check.
Nothing here writes to a host. The reboots themselves stay with the operator,
behind the single approval `/roll` asks for.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import click

from ..hosts import HostRegistry, default_registry
from ..module_support import connection_for_host
from ..modules.pve_guest.inventory import pve_nodes
from . import checks
from .checks import STOP, Finding
from .facts import NodeFacts, Unreachable, VmFacts, node_facts, vm_facts
from .parse import SECTION_MARK, sections, task_names, up_values
from .plan import Inventory, Survey, build_plan, render, scoped
from .windows import load_windows, window_findings

TEXTFILE = "/var/lib/prometheus/node-exporter"
METRICS_STACK = "victoria-metrics"
CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 180
VERIFY_INTERVAL = 20
PBS_TASKS = "proxmox-backup-manager task list --output-format json"
# The metrics host's own readiness: an exited container there blinds every check.
MONITOR_READY = [
    ("alertmanager", "localhost:9093/-/ready"),
    ("victoriametrics", "localhost:8428/health"),
]

# Shared by nodes and VMs: did the automation run, and what would it still install.
# The Inst lines name the packages, which is how a not-yet-installed kernel shows.
UPGRADE_READINGS = [
    (
        "upgrade",
        "systemctl show homelab-apt-dist-upgrade.service -p LoadState -p Result -p ExecMainStatus",
    ),
    (
        "apt",
        "apt-get -s -o DPkg::Lock::Timeout=60 dist-upgrade 2>/dev/null "
        "| grep -E '^Inst |upgraded,'",
    ),
    (
        "lists",
        "find /var/lib/apt/lists -maxdepth 1 -name '*_Packages' -printf '%T@\\n' "
        "| sort -n | tail -n 1",
    ),
    ("epoch", "date +%s"),
]

# Every enabled service's state, for checks.units; `systemctl` may carry `--user -M`.
# Templates are skipped: their instances are what runs, and they list as enabled.
SERVICE_PROPS = "-p Id -p Type -p ActiveState -p ConditionResult -p TriggeredBy"
ONBOOT = "grep -l '^onboot: 1' /etc/pve/local/lxc/*.conf /etc/pve/local/qemu-server/*.conf"


def enabled_services(systemctl: str) -> str:
    return (
        f"units=$({systemctl} list-unit-files --type=service --state=enabled --no-legend "
        "--plain 2>/dev/null | awk '{print $1}' | grep -v '@[.]service$'); "
        f'[ -z "$units" ] || {systemctl} show {SERVICE_PROPS} $units 2>/dev/null'
    )


# (host, command) -> stdout; raises HostDown.
Runner = Callable[[str, str], str]


class HostDown(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------


def guest_vmids(registry: HostRegistry) -> dict[str, int]:
    vmids = {}
    for host in registry.list_hosts():
        block = registry.get(host, "pve-guest", None)
        if isinstance(block, dict) and "vmid" in block:
            vmids[host] = int(block["vmid"])
    return vmids


def own_kernel_vms(registry: HostRegistry, vmids: dict[str, int]) -> list[str]:
    """qemu guests whose own kernel apt upgrades; a node reboot only migrates them."""
    return [
        host
        for host in vmids
        if registry.get(host, "pve-guest.kind", None) == "qemu"
        and registry.get(host, "metrics-exporters.own_kernel", False) is True
    ]


def metrics_host(registry: HostRegistry) -> str | None:
    for host in registry.list_hosts(feature="docker-stacks"):
        if METRICS_STACK in (registry.get(host, "docker-stacks.stacks", []) or []):
            return host
    return None


def load_inventory(registry: HostRegistry) -> Inventory:
    vmids = guest_vmids(registry)
    vms = own_kernel_vms(registry, vmids)
    return Inventory(
        nodes=pve_nodes(registry),
        vms=vms,
        vmids=vmids,
        pbs=[h for h in registry.list_hosts() if registry.get(h, "config.type") == "pbs"],
        vips={
            host: [str(vip).split()[0] for vip in registry.get(host, "keepalived.virtual_ips", [])]
            for host in vms
        },
        users={
            host: str(registry.get(host, "config.ssh_config.user"))
            for host in vms
            if registry.get(host, "config.ssh_config.user", None)
        },
        monitor=metrics_host(registry),
        upgraders=registry.list_hosts(feature="pve-upgrade"),
    )


# --------------------------------------------------------------------------------------
# Remote reads
# --------------------------------------------------------------------------------------


def script(readings: list[tuple[str, str]]) -> str:
    lines = [f"printf '\\n{SECTION_MARK}%s\\n' {name}; {command}" for name, command in readings]
    return "\n".join([*lines, "true"])


def node_script(host: str) -> str:
    return script(
        [
            ("kernel", f"cat {TEXTFILE}/reboot.prom 2>/dev/null"),
            ("boot", f"cat {TEXTFILE}/boot-entries.prom 2>/dev/null"),
            ("uname", "uname -r"),
            ("uptime", "cut -d' ' -f1 /proc/uptime"),
            ("pools", "zpool status -x 2>&1"),
            ("quorum", "pvecm status 2>&1"),
            ("ha", "ha-manager status 2>&1"),
            (
                "resources",
                "pvesh get /cluster/resources --type vm --output-format json 2>/dev/null",
            ),
            ("replication", "pvesr status 2>&1"),
            ("clock", "date +%Y-%m-%d_%H:%M:%S"),
            ("ups", "apcaccess status 2>/dev/null"),
            (
                "tasks",
                f"pvesh get /nodes/{shlex.quote(host)}/tasks --source active "
                "--output-format json 2>/dev/null",
            ),
            *UPGRADE_READINGS,
            ("exporter", "systemctl is-active prometheus-node-exporter"),
            ("failed", "systemctl --failed --no-legend --plain"),
            ("enabled", enabled_services("systemctl")),
            ("onboot", f"test -d /etc/pve/local && echo listed && {ONBOOT} 2>/dev/null"),
        ]
    )


def vm_script(user: str | None) -> str:
    readings = [
        ("kernel", f"cat {TEXTFILE}/reboot.prom 2>/dev/null"),
        ("uname", "uname -r"),
        ("uptime", "cut -d' ' -f1 /proc/uptime"),
        (
            "swarm",
            "docker node ls --format "
            "'{{.Hostname}}\t{{.Status}}\t{{.ManagerStatus}}\t{{.Self}}' 2>/dev/null",
        ),
        ("addresses", "ip -br addr 2>/dev/null"),
        # A busy GitHub Actions runner has a Runner.Worker process; the bracket
        # keeps pgrep from matching this script's own command line.
        ("ci", "pgrep -c -f 'Runner[.]Worker'"),
        ("failed", "systemctl --failed --no-legend --plain"),
        ("enabled", enabled_services("systemctl")),
        *UPGRADE_READINGS,
        # -a: an exited or never-started container is the failure this looks for.
        ("containers", "docker ps -a --format '{{.Names}}\t{{.Status}}' 2>/dev/null"),
    ]
    if user:
        user_ctl = f"systemctl --user -M {shlex.quote(f'{user}@')}"
        readings += [
            ("userfailed", f"{user_ctl} --failed --no-legend --plain 2>/dev/null"),
            ("linger", f"loginctl show-user {shlex.quote(user)} -p Linger --value 2>/dev/null"),
            ("usermanager", f"{user_ctl} is-system-running 2>/dev/null"),
            ("userenabled", enabled_services(user_ctl)),
        ]
    return script(readings)


def up_command(host: str) -> str:
    query = shlex.quote(f'query=up{{host="{host}"}}')
    return f"curl -s --max-time 10 --get localhost:8428/api/v1/query --data-urlencode {query}"


def ready_command() -> str:
    return script(
        [
            (name, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 {url}")
            for name, url in MONITOR_READY
        ]
    )


def ssh_runner(root: Path) -> Runner:
    connections: dict = {}

    def run(host: str, command: str) -> str:
        if host not in connections:
            connection = connection_for_host(root, host).connection
            connection.connect_timeout = CONNECT_TIMEOUT
            connections[host] = connection
        try:
            result = connections[host].run(command, hide=True, warn=True, timeout=COMMAND_TIMEOUT)
        except Exception as exc:  # DNS, refused, timeout, auth: all mean "not answering"
            connections.pop(host, None)
            raise HostDown(f"{type(exc).__name__}: {exc}".strip()) from exc
        if result.exited != 0:
            raise HostDown(f"survey script exited {result.exited}")
        return result.stdout

    return run


def read(runner: Runner, host: str, command: str) -> dict[str, str] | Unreachable:
    try:
        return sections(runner(host, command))
    except HostDown as exc:
        return Unreachable(host, str(exc))


def survey_node(runner: Runner, host: str, standalone: bool) -> NodeFacts | Unreachable:
    found = read(runner, host, node_script(host))
    return found if isinstance(found, Unreachable) else node_facts(host, standalone, found)


def survey_vm(runner: Runner, inventory: Inventory, host: str) -> VmFacts | Unreachable:
    found = read(runner, host, vm_script(inventory.users.get(host)))
    return found if isinstance(found, Unreachable) else vm_facts(host, found)


def pbs_tasks(runner: Runner, host: str) -> list[str] | None:
    try:
        return task_names(runner(host, PBS_TASKS))
    except HostDown:
        return None


def take_survey(inventory: Inventory, runner: Runner, scope: list[str]) -> Survey:
    """Every node is read even for a subset: tower's placement and HA are cluster facts."""
    return Survey(
        nodes={host: survey_node(runner, host, alone) for host, alone in inventory.nodes.items()},
        vms={host: survey_vm(runner, inventory, host) for host in inventory.vms if host in scope},
        pbs_tasks={host: pbs_tasks(runner, host) for host in inventory.pbs},
    )


# --------------------------------------------------------------------------------------
# ./roll plan
# --------------------------------------------------------------------------------------


def run_plan(
    root: Path,
    requested: list[str],
    as_json: bool,
    runner: Runner | None = None,
    now: datetime | None = None,
) -> int:
    inventory = load_inventory(default_registry(root))
    scope = scoped(inventory, requested)
    survey = take_survey(inventory, runner or ssh_runner(root), scope)
    window = window_findings(load_windows(root), now or datetime.now(UTC))
    plan = build_plan(inventory, survey, scope, window)
    if as_json:
        click.echo(json.dumps(plan.as_dict(), indent=2))
    else:
        click.echo("\n".join(render(plan)))
    return 1 if plan.verdict == "stop" else 0


# --------------------------------------------------------------------------------------
# ./roll verify
# --------------------------------------------------------------------------------------


def scrape_state(runner: Runner, inventory: Inventory, host: str) -> list[Finding]:
    values = None
    if inventory.monitor:
        try:
            values = up_values(runner(inventory.monitor, up_command(host)))
        except HostDown:
            values = None
    return checks.scraped(host, values, inventory.monitor)


def summary(facts: NodeFacts | VmFacts) -> str:
    uptime = f"up {int(facts.uptime // 60)} min" if facts.uptime is not None else "uptime ?"
    return f"{facts.uname or '?'}, {uptime}"


def verify_host(runner: Runner, inventory: Inventory, host: str) -> tuple[str, list[Finding]]:
    if host in inventory.nodes:
        facts: NodeFacts | VmFacts | Unreachable = survey_node(runner, host, inventory.nodes[host])
    else:
        facts = survey_vm(runner, inventory, host)
    if isinstance(facts, Unreachable):
        return "unreachable", [checks.stop(host, f"unreachable: {facts.error}")]
    if isinstance(facts, NodeFacts):
        findings = checks.node_recovered(facts)
    else:
        findings = checks.vm_recovered(facts)
    return summary(facts), findings + scrape_state(runner, inventory, host)


def monitor_state(runner: Runner, monitor: str) -> list[Finding]:
    found = read(runner, monitor, ready_command())
    if isinstance(found, Unreachable):
        return [checks.stop(monitor, f"monitoring is unreachable: {found.error}")]
    return checks.monitor_ready(monitor, {name: found.get(name, "") for name, _ in MONITOR_READY})


def verify_once(
    runner: Runner, inventory: Inventory, hosts: list[str]
) -> dict[str, tuple[str, list[Finding]]]:
    """Every pass also checks the metrics host, wave target or not: a node wave
    can migrate it, and without it no host's `up` check means anything."""
    results = {host: verify_host(runner, inventory, host) for host in hosts}
    monitor = inventory.monitor
    if not monitor:
        return results
    state, findings = results.get(monitor, ("monitoring", []))
    if state != "unreachable":
        findings = findings + monitor_state(runner, monitor)
    results[monitor] = (state, findings)
    return results


def failing(results: dict[str, tuple[str, list[Finding]]]) -> list[str]:
    return [
        host for host, (_, findings) in results.items() if any(f.level == STOP for f in findings)
    ]


def verify_lines(results: dict[str, tuple[str, list[Finding]]]) -> list[str]:
    lines = []
    for host, (state, findings) in results.items():
        verdict = "FAIL" if any(f.level == STOP for f in findings) else "ok"
        lines.append(f"{host}: {verdict} ({state})")
        lines += [f"  {item.level}: {item.message}" for item in findings]
    return lines


def run_verify(
    root: Path,
    hosts: list[str],
    wait: int,
    as_json: bool,
    runner_factory: Callable[[], Runner] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    inventory = load_inventory(default_registry(root))
    targets = scoped(inventory, hosts)
    factory = runner_factory or (lambda: ssh_runner(root))
    deadline = clock() + wait
    while True:
        # A fresh runner each pass: connections opened before a reboot are dead.
        results = verify_once(factory(), inventory, targets)
        pending = failing(results)
        if not pending or clock() >= deadline:
            break
        click.echo(f"waiting on {', '.join(pending)}; re-checking in {VERIFY_INTERVAL}s", err=True)
        sleep(VERIFY_INTERVAL)
    if as_json:
        payload = {
            host: {"state": state, "findings": [vars(f) for f in findings]}
            for host, (state, findings) in results.items()
        }
        click.echo(json.dumps({"ok": not pending, "hosts": payload}, indent=2))
    else:
        click.echo("\n".join(verify_lines(results)))
    return 1 if pending else 0
