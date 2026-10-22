"""`./roll`: the rolling-reboot planner and post-reboot verifier, both read-only.

The command samples below are real output, captured on ace, bray, clovis, osiris,
helm and xur on 2026-10-21, trimmed. The fake cluster built from them is what
every plan and verify test runs against, so a parser change that no longer reads
what the hosts actually print fails here rather than on a Saturday.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from homelab import cli
from homelab.hosts import HostRegistry
from homelab.roll import checks, facts, parse, plan, survey, windows
from homelab.roll.checks import NOTE, STOP, WARN, Finding

ROOT = Path(__file__).resolve().parents[1]

PVECM = """\
Cluster information
-------------------
Name:             net-cluster
Config Version:   31

Quorum information
------------------
Date:             Wed Oct 21 21:44:18 2026
Quorum provider:  corosync_votequorum
Nodes:            3
Quorate:          Yes

Votequorum information
----------------------
Expected votes:   3
Highest expected: 3
Total votes:      3
Quorum:           2
Flags:            Quorate
"""
PVECM_STANDALONE = (
    "Error: Corosync config '/etc/pve/corosync.conf' does not exist - "
    "is this node part of a cluster?"
)
HA = """\
quorum OK
master ace (active, Wed Oct 21 21:44:13 2026)
fencing armed (CRM watchdog active)
lrm ace (active, watchdog active, Wed Oct 21 21:44:16 2026)
lrm bray (active, watchdog active, Wed Oct 21 21:44:16 2026)
lrm clovis (idle, Wed Oct 21 21:44:15 2026)
service ct:101 (ace, started)
service ct:104 (bray, started)
service ct:108 (bray, started)
service vm:109 (ace, started)
service vm:110 (bray, started)
service vm:112 (clovis, started)
"""
PVESR = """\
JobID      Enabled    Target                           LastSync             NextSync   Duration  FailCount State
104-0      Yes        local/ace             2026-10-21_21:30:02  2026-10-21_21:45:00    2.73225          0 OK
110-0      Yes        local/ace             2026-10-21_21:30:12  2026-10-21_21:45:00   4.274788          0 OK
"""  # noqa: E501 -- byte-identical to `pvesr status`
PVESR_EMPTY = "JobID      Enabled    Target                           LastSync             NextSync   Duration  FailCount State"  # noqa: E501
CLOCK = "2026-10-21_21:44:20"
BOOT_OK = """\
# HELP homelab_boot_entries_dead Active UEFI boot entries whose device path cannot load anything.
# TYPE homelab_boot_entries_dead gauge
homelab_boot_entries_dead{host="ace"} 0
homelab_boot_first_entry_loadable{host="ace"} 1
"""
UPS_ONLINE = "STATUS   : ONLINE \nBCHARGE  : 100.0 Percent\nTIMELEFT : 38.1 Minutes"
UPGRADE_OK = "Result=success\nExecMainStatus=0\nLoadState=loaded"
APT_NONE = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."
APT_KERNEL = """\
Inst proxmox-kernel-7.0.14-26-pve-signed (7.0.14-26 Proxmox:9.0/stable [amd64])
Inst proxmox-kernel-7.0 [7.0.14-25] (7.0.14-26 Proxmox:9.0/stable [all])
1 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.
"""
SWARM = "helm\tReady\tReachable\ttrue\nneo\tReady\tReachable\tfalse\ntower\tReady\tLeader\tfalse"
ADDR = (
    "lo UNKNOWN 127.0.0.1/8\n"
    "nic0 UP 10.0.40.245/24 metric 1024 10.0.40.15/32 fe80::be24:11ff:fe18:e317/64"
)
OLD, NEW = "7.0.14-24-pve", "7.0.14-25-pve"
VM_OLD, VM_NEW = "6.12.112+deb13-amd64", "6.12.113+deb13-amd64"

RESOURCES = [
    {
        "vmid": 101,
        "name": "tower",
        "node": "ace",
        "status": "running",
        "type": "lxc",
        "hastate": "started",
    },
    {"vmid": 102, "name": "void", "node": "clovis", "status": "stopped", "type": "qemu"},
    {
        "vmid": 104,
        "name": "arc",
        "node": "bray",
        "status": "running",
        "type": "lxc",
        "hastate": "started",
    },
    {
        "vmid": 109,
        "name": "zion",
        "node": "ace",
        "status": "running",
        "type": "qemu",
        "hastate": "started",
    },
    {
        "vmid": 110,
        "name": "riven",
        "node": "bray",
        "status": "running",
        "type": "qemu",
        "hastate": "started",
    },
    {
        "vmid": 112,
        "name": "helm",
        "node": "clovis",
        "status": "running",
        "type": "qemu",
        "hastate": "started",
    },
]
OSIRIS_RESOURCES = [
    {"vmid": 105, "name": "xur", "node": "osiris", "status": "running", "type": "lxc"},
    {"vmid": 111, "name": "deepstone", "node": "osiris", "status": "running", "type": "lxc"},
]
ONBOOT = {
    "ace": "listed\n/etc/pve/local/lxc/101.conf\n/etc/pve/local/qemu-server/109.conf",
    "bray": "listed\n/etc/pve/local/lxc/104.conf\n/etc/pve/local/qemu-server/110.conf",
    "clovis": "listed\n/etc/pve/local/qemu-server/112.conf",
    "osiris": "listed\n/etc/pve/local/lxc/105.conf\n/etc/pve/local/lxc/111.conf",
}
# `systemctl show` over the enabled services: one up, one socket-activated, one
# oneshot, one skipped by its condition. None of them is down.
ENABLED = """\
Type=notify
Id=ssh.service
TriggeredBy=
ActiveState=active
ConditionResult=yes

Type=exec
Id=netavark-dhcp-proxy.service
TriggeredBy=netavark-dhcp-proxy.socket
ActiveState=inactive
ConditionResult=yes

Type=oneshot
Id=grub-common.service
TriggeredBy=
ActiveState=inactive
ConditionResult=yes

Type=notify
Id=smartmontools.service
TriggeredBy=
ActiveState=inactive
ConditionResult=no
"""
USER_ENABLED = """\
Type=simple
Id=opencode.service
TriggeredBy=
ActiveState=active
ConditionResult=yes

Type=simple
Id=github-runner.service
TriggeredBy=
ActiveState=active
ConditionResult=yes
"""


def reboot_prom(host: str, running: str, installed: str, required: int | None) -> str:
    lines = [f'homelab_kernel_info{{host="{host}",running="{running}",installed="{installed}"}} 1']
    if required is not None:
        lines.append(f'homelab_reboot_required{{host="{host}"}} {required}')
    return "# HELP homelab_kernel_info x\n" + "\n".join(lines)


def output(readings: dict[str, str]) -> str:
    return "".join(f"\n@@{name}\n{text}\n" for name, text in readings.items())


def node_readings(host: str, pending: bool = False, **overrides: str) -> dict[str, str]:
    standalone = host == "osiris"
    running = OLD if pending else NEW
    readings = {
        "kernel": reboot_prom(host, running, NEW, int(pending)),
        "boot": BOOT_OK,
        "uname": running,
        "uptime": "218201.31",
        "pools": "all pools are healthy",
        "quorum": PVECM_STANDALONE if standalone else PVECM,
        "ha": "quorum OK\nfencing standby (CRM watchdog standby)" if standalone else HA,
        "resources": json.dumps(OSIRIS_RESOURCES if standalone else RESOURCES),
        "replication": PVESR_EMPTY if standalone else PVESR,
        "clock": CLOCK,
        "ups": UPS_ONLINE,
        "tasks": "[]",
        "upgrade": UPGRADE_OK,
        "apt": APT_NONE,
        "lists": "1792616825.0000000000",
        "epoch": "1792634266",
        "exporter": "active",
        "failed": "",
        "enabled": ENABLED,
        "onboot": ONBOOT[host],
    }
    readings.update(overrides)
    return readings


def vm_readings(host: str, pending: bool = False, **overrides: str) -> dict[str, str]:
    running = VM_OLD if pending else VM_NEW
    readings = {
        "kernel": reboot_prom(host, running, VM_NEW, int(pending)),
        "uname": running,
        "uptime": "600",
        "swarm": SWARM if host == "helm" else "",
        "addresses": ADDR if host == "helm" else "",
        "ci": "0",
        "failed": "",
        "enabled": ENABLED,
        "upgrade": UPGRADE_OK,
        "apt": APT_NONE,
        "lists": "1792616825",
        "epoch": "1792634266",
        "containers": "grafana\tUp 2 hours (healthy)" if host == "helm" else "",
    }
    if host == "riven":
        readings |= {
            "userfailed": "",
            "linger": "yes",
            "usermanager": "running",
            "userenabled": USER_ENABLED,
        }
    readings.update(overrides)
    return readings


class Cluster:
    """A scripted fleet: each host answers its survey script, or is down."""

    def __init__(self, pending: tuple[str, ...] = (), **hosts: dict[str, str]) -> None:
        self.hosts: dict[str, dict[str, str] | None] = {}
        for node in INVENTORY.nodes:
            self.hosts[node] = node_readings(node, node in pending)
        for vm in ("helm", "riven"):
            self.hosts[vm] = vm_readings(vm, vm in pending)
        self.hosts.update(hosts)
        self.pbs = "[]"
        self.up = {"data": {"result": [{"value": [0, "1"]}]}}
        self.ready = {"alertmanager": "200", "victoriametrics": "200"}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, host: str, command: str) -> str:
        self.calls.append((host, command))
        if command == survey.PBS_TASKS:
            return self.pbs
        readings = self.hosts.get(host)
        if readings is None:
            raise survey.HostDown(f"{host}: no route to host")
        if "api/v1/query" in command:
            return json.dumps(self.up)
        if command == survey.ready_command():
            return output(self.ready)
        return output(readings)


INVENTORY = plan.Inventory(
    nodes={"ace": False, "bray": False, "clovis": False, "osiris": True},
    vms=["riven", "helm", "shaxx"],
    vmids={"tower": 101, "arc": 104, "xur": 105, "riven": 110, "helm": 112, "shaxx": 113},
    pbs=["xur"],
    vips={"riven": [], "helm": ["10.0.40.15"], "shaxx": []},
    users={"riven": "freender"},
    monitor="helm",
    upgraders=["ace", "bray", "clovis", "osiris"],
)
SCOPE = plan.scoped(INVENTORY, [])


def make_plan(cluster: Cluster, scope: list[str] = SCOPE, window=()) -> plan.Plan:
    taken = survey.take_survey(INVENTORY, cluster, scope)
    return plan.build_plan(INVENTORY, taken, scope, list(window))


def messages(findings: list[Finding]) -> list[str]:
    return [f"{item.host}: {item.message}" for item in findings]


# --------------------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------------------


def test_sections_split_on_markers_and_ignore_preamble() -> None:
    text = "motd noise\n@@a\none\ntwo\n\n@@b\n@@c\nthree\n"
    assert parse.sections(text) == {"a": "one\ntwo", "b": "", "c": "three"}


def test_numbers_refuse_garbage() -> None:
    assert parse.to_int(" 3 ") == 3
    assert parse.to_int("x") is None
    assert parse.to_int(None) is None
    assert parse.to_float("2.5") == 2.5
    assert parse.to_float("") is None


def test_prom_sample_reads_labels_and_skips_comments() -> None:
    text = '# HELP a_b x\nother 2\na_b{host="ace",x="y"} 3\na_b{host="bray"} 4'
    assert parse.prom_sample(text, "a_b") == parse.Sample({"host": "ace", "x": "y"}, 3.0)
    assert parse.prom_sample("plain 7", "plain") == parse.Sample({}, 7.0)
    assert parse.prom_sample("a_b{} NaNx", "a_b") is None
    assert parse.prom_sample(text, "missing") is None


def test_kernel_reading_and_its_absence() -> None:
    kernel = parse.parse_kernel(reboot_prom("ace", OLD, NEW, 1))
    assert kernel == parse.Kernel(True, OLD, NEW)
    assert parse.parse_kernel(reboot_prom("ace", NEW, NEW, 0)).required is False
    assert parse.parse_kernel(reboot_prom("ace", NEW, NEW, None)).required is None
    assert parse.parse_kernel("") == parse.Kernel(None, "", "")


def test_boot_reading() -> None:
    assert parse.parse_boot(BOOT_OK) == parse.Boot(0, True)
    bad = 'homelab_boot_entries_dead{host="a"} 2\nhomelab_boot_first_entry_loadable{host="a"} 0'
    assert parse.parse_boot(bad) == parse.Boot(2, False)
    assert parse.parse_boot("") == parse.Boot(None, None)


def test_quorum_from_real_pvecm() -> None:
    assert parse.parse_quorum(PVECM) == parse.Quorum(True, 3, 3)
    degraded = PVECM.replace("Quorate:          Yes", "Quorate:          No").replace(
        "Total votes:      3", "Total votes:      1"
    )
    assert parse.parse_quorum(degraded) == parse.Quorum(False, 1, 3)
    assert parse.parse_quorum(PVECM_STANDALONE) == parse.Quorum(False, None, None)


def test_ha_from_real_status() -> None:
    status = parse.parse_ha(HA + "service vm:113 (ace, migrate)\nservice ct:9 (bray)\n")
    assert status.quorum_ok
    assert status.lrms == {"ace": "active", "bray": "active", "clovis": "idle"}
    assert status.services["ct:101"] == parse.HaService("ct:101", "ace", "started")
    assert status.services["vm:113"].state == "migrate"
    assert status.services["ct:9"].state == ""
    assert not parse.parse_ha("lrm ace (active)").quorum_ok


def test_replication_from_real_pvesr() -> None:
    jobs = parse.parse_replication(PVESR)
    assert [job.job for job in jobs] == ["104-0", "110-0"]
    assert jobs[0] == parse.ReplJob(
        "104-0", True, "local/ace", datetime(2026, 10, 21, 21, 30, 2), 0, "OK"
    )
    failed = parse.parse_replication("105-0 No local/x - - - 3 error: command 'zfs send' failed")
    assert failed[0].enabled is False
    assert failed[0].last_sync is None
    assert failed[0].fail_count == 3
    assert failed[0].state == "error: command 'zfs send' failed"
    assert parse.parse_replication(PVESR_EMPTY) == []
    assert parse.parse_replication("bash: pvesr: command not found") == []


def test_task_names_for_pve_and_pbs() -> None:
    pve = [{"type": "vzdump", "id": "101"}, {"type": "aptupdate", "id": ""}]
    pbs = [{"worker_type": "syncjob", "worker_id": "cottonwood-pull"}, "junk"]
    assert parse.task_names(json.dumps(pve)) == ["vzdump 101", "aptupdate"]
    assert parse.task_names(json.dumps(pbs)) == ["syncjob cottonwood-pull"]
    assert parse.task_names(json.dumps([{}])) == ["task"]
    assert parse.task_names("[]") == []
    assert parse.task_names("") is None
    assert parse.task_names('{"a": 1}') is None


def test_properties_and_apt_summary() -> None:
    assert parse.properties(UPGRADE_OK + "\nnoise") == {
        "Result": "success",
        "ExecMainStatus": "0",
        "LoadState": "loaded",
    }
    assert parse.apt_pending(APT_NONE) == 0
    assert parse.apt_pending("3 upgraded, 1 newly installed, 2 to remove and 0 not upgraded.") == 6
    assert parse.apt_pending("E: Could not get lock") is None


@pytest.mark.parametrize(
    ("package", "release"),
    [
        ("proxmox-kernel-7.0.14-26-pve-signed", "7.0.14-26-pve"),
        ("proxmox-kernel-7.0.14-26-pve", "7.0.14-26-pve"),
        ("linux-image-6.12.114+deb13-amd64", "6.12.114+deb13-amd64"),
        ("linux-image-unsigned-6.8.0-50-generic", "6.8.0-50-generic"),
        ("linux-image-6.8.0-50-generic-unsigned", "6.8.0-50-generic"),
        ("linux-image-amd64", None),
        ("linux-image-cloud-amd64", None),
        ("proxmox-kernel-helper", None),
        ("proxmox-kernel-7.0", None),
        ("linux-image-6.12.114+deb13-amd64-dbg", None),
        ("linux-image-6.12.114+deb13-amd64-dbgsym", None),
        ("6.12.114+deb13-amd64", None),
        ("libc6", None),
    ],
)
def test_kernel_release_follows_the_exporter_rule(package: str, release: str | None) -> None:
    assert parse.kernel_release(package) == release


def test_apt_changes_name_the_kernels() -> None:
    assert parse.apt_changes(APT_KERNEL) == parse.AptChanges(2, ["7.0.14-26-pve"])
    assert parse.apt_changes(APT_NONE) == parse.AptChanges(0, [])
    assert parse.apt_changes("") is None


def test_swarm_units_containers_and_up() -> None:
    assert parse.parse_swarm(SWARM)[0] == parse.SwarmNode("helm", "Ready", "Reachable", True)
    assert parse.parse_swarm("Error response from daemon: not a swarm manager") == []
    assert parse.unit_names("x.service loaded failed failed X\n\n") == ["x.service"]
    assert parse.unhealthy_containers(
        "a\tUp 2 hours (healthy)\nb\tRestarting (1) 5 seconds ago\n"
        "c\tUp 1 minute (unhealthy)\nd\tUp 3 seconds (health: starting)\ne\tUp 1 day"
    ) == [
        "b (Restarting (1) 5 seconds ago)",
        "c (Up 1 minute (unhealthy))",
        "d (Up 3 seconds (health: starting))",
    ]
    assert parse.up_values(json.dumps({"data": {"result": [{"value": [1, "0"]}]}})) == [0.0]
    assert parse.up_values(json.dumps({"data": {"result": []}})) == []
    assert parse.up_values("") is None
    assert parse.up_values('{"data": {"result": [{"value": []}]}}') is None


DOWN = "Type=notify\nId=pveproxy.service\nTriggeredBy=\nActiveState=activating\nConditionResult=yes"


def test_down_services_skip_oneshots_conditions_and_sockets() -> None:
    assert parse.down_services(ENABLED) == []
    assert parse.down_services(ENABLED + "\n" + DOWN) == ["pveproxy.service"]
    assert parse.down_services("Type=simple\nActiveState=failed") == ["?"]
    assert parse.down_services("") is None
    assert parse.down_services("\n\n") is None


@pytest.mark.parametrize(
    ("override", "down"),
    [
        ({}, True),
        ({"ActiveState": "active"}, False),
        ({"Type": "oneshot"}, False),
        ({"ConditionResult": "no"}, False),
        ({"TriggeredBy": "x.socket"}, False),
        ({"ActiveState": "deactivating"}, True),
        ({"ConditionResult": ""}, True),
    ],
)
def test_service_down(override: dict[str, str], down: bool) -> None:
    unit = {"Type": "simple", "ActiveState": "inactive", "ConditionResult": "yes"}
    assert parse.service_down(unit | {"TriggeredBy": ""} | override) is down


def test_onboot_guests() -> None:
    assert parse.onboot_guests(ONBOOT["osiris"]) == [105, 111]
    assert parse.onboot_guests("listed\n/etc/pve/local/qemu-server/9.conf\n/x/junk.conf") == [9]
    assert parse.onboot_guests("listed") == []
    assert parse.onboot_guests("") is None
    assert parse.onboot_guests("/etc/pve/local/lxc/105.conf") is None


def test_facts_age_needs_both_clocks() -> None:
    assert facts.age("100", "160.5") == 60.5
    assert facts.age("", "160") is None
    assert facts.age("100", "") is None


# --------------------------------------------------------------------------------------
# checks: pre-flight
# --------------------------------------------------------------------------------------


def node(host: str = "ace", pending: bool = True, **overrides: str) -> facts.NodeFacts:
    return facts.node_facts(host, host == "osiris", node_readings(host, pending, **overrides))


def vm(host: str = "helm", pending: bool = True, **overrides: str) -> facts.VmFacts:
    return facts.vm_facts(host, vm_readings(host, pending, **overrides))


REMEDY = "./deploy --confirm-upgrade pve-upgrade ace"


def test_a_healthy_node_has_no_findings() -> None:
    assert checks.node_preflight(node(), REMEDY) == []
    assert checks.node_preflight(node("osiris"), REMEDY) == []


def test_missing_reboot_reading_stops() -> None:
    found = checks.node_preflight(node(kernel=""), REMEDY)
    assert messages(found) == ["ace: the reboot exporter has no reading (reboot.prom)"]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (PVECM.replace("Quorate:          Yes", "Quorate:          No"), "cluster is not quorate"),
        (PVECM.replace("Total votes:      3", "Total votes:      2"), "has 2 of 3 expected votes"),
        (PVECM.replace("Expected votes:   3", ""), "has 3 of None expected votes"),
    ],
)
def test_quorum_stops(override: str, message: str) -> None:
    found = checks.quorum(node(quorum=override))
    assert len(found) == 1
    assert found[0].level == STOP
    assert message in found[0].message


def test_standalone_node_skips_quorum() -> None:
    assert checks.quorum(node("osiris", quorum="garbage")) == []


def test_replication_problems() -> None:
    clock = datetime(2026, 10, 21, 22, 0, 0)

    def job(**kw) -> parse.ReplJob:
        base = dict(job="1-0", enabled=True, target="t", last_sync=clock, fail_count=0, state="OK")
        return parse.ReplJob(**{**base, **kw})

    assert checks.replication_problem(job(), clock) is None
    assert checks.replication_problem(job(enabled=False, state="bad"), clock) is None
    assert checks.replication_problem(job(state="error"), clock) == "error, 0 failure(s)"
    assert checks.replication_problem(job(fail_count=2), clock) == "OK, 2 failure(s)"
    assert checks.replication_problem(job(last_sync=None), clock) == "never synced"
    assert checks.replication_problem(job(), None) == "never synced"
    old = clock - timedelta(hours=2, seconds=1)
    assert checks.replication_problem(job(last_sync=old), clock) == "last sync 2:00:01 ago"
    edge = clock - timedelta(hours=2)
    assert checks.replication_problem(job(last_sync=edge), clock) is None


def test_replication_findings_name_the_job() -> None:
    stale = PVESR.replace("2026-10-21_21:30:02", "2026-10-21_18:00:00")
    assert messages(checks.replication(node(replication=stale))) == [
        "ace: replication 104-0 -> local/ace: last sync 3:44:20 ago"
    ]
    assert messages(checks.replication(node(clock="?"))) == [
        "ace: cannot read the node clock to age replication"
    ]
    assert checks.replication(node("osiris", clock="?")) == []


def test_pools() -> None:
    assert checks.pools("ace", "all pools are healthy\n") == []
    degraded = "  pool: tank\n state: DEGRADED"
    assert messages(checks.pools("ace", degraded)) == ["ace: zpool status -x: pool: tank"]
    assert messages(checks.pools("ace", "")) == ["ace: zpool status -x: no output"]


def test_boot_entries() -> None:
    assert checks.boot(node()) == []
    no_reading = checks.boot(node(boot=""))
    assert "no reading" in no_reading[0].message
    half = checks.boot(node(boot='homelab_boot_first_entry_loadable{host="a"} 1'))
    assert "no reading" in half[0].message
    unloadable = (
        'homelab_boot_entries_dead{host="a"} 0\nhomelab_boot_first_entry_loadable{host="a"} 0'
    )
    assert "efibootmgr" in checks.boot(node(boot=unloadable))[0].message
    dead = 'homelab_boot_entries_dead{host="a"} 2\nhomelab_boot_first_entry_loadable{host="a"} 1'
    assert messages(checks.boot(node(boot=dead))) == [
        "ace: 2 dead UEFI boot entry(ies); clean them first"
    ]


def test_ups() -> None:
    assert checks.ups(node()) == []
    assert checks.ups(node(ups="")) == []
    assert messages(checks.ups(node(ups="STATUS   : ONBATT"))) == ["ace: UPS status is ONBATT"]


def test_tasks() -> None:
    assert checks.tasks("ace", []) == []
    assert messages(checks.tasks("ace", None)) == ["ace: cannot read running tasks"]
    assert messages(checks.tasks("osiris", ["syncjob x"], owner="xur")) == [
        "osiris: task(s) running on xur: syncjob x"
    ]


def test_upgrade_unit() -> None:
    assert checks.upgrade_unit("a", {"Result": "success"}) == []
    assert checks.upgrade_unit("a", {"LoadState": "not-found", "Result": "x"})[0].level == WARN
    assert messages(checks.upgrade_unit("a", {"Result": "exit-code"})) == [
        "a: last homelab-apt-dist-upgrade result: exit-code"
    ]


def test_a_newer_kernel_than_installed_stops_with_the_remedy() -> None:
    kernel = parse.Kernel(True, OLD, NEW)
    (found,) = checks.available("ace", parse.apt_changes(APT_KERNEL), kernel, REMEDY)
    assert found.level == STOP
    assert found.message == (
        "kernel 7.0.14-26-pve is available but not installed: a reboot now boots "
        f"{NEW} and needs another once it lands. Install it first ({REMEDY}), then plan again"
    )


def test_the_installed_kernel_reappearing_is_not_newer() -> None:
    kernel = parse.Kernel(True, OLD, "7.0.14-26-pve")
    found = checks.available("ace", parse.apt_changes(APT_KERNEL), kernel, REMEDY)
    assert messages(found) == ["ace: 2 non-kernel package change(s) available"]
    assert found[0].level == WARN


def test_other_packages_only_warn_and_none_is_silent() -> None:
    kernel = parse.Kernel(True, OLD, NEW)
    assert checks.available("a", parse.AptChanges(0, []), kernel, REMEDY) == []
    assert checks.available("a", parse.AptChanges(1, []), kernel, REMEDY)[0].level == WARN
    assert checks.available("a", None, kernel, REMEDY)[0].level == STOP


def test_stale_lists_warn() -> None:
    assert checks.lists("a", 48 * 3600) == []
    assert messages(checks.lists("a", 48 * 3600 + 1)) == [
        "a: apt package lists are 48 h old; a new kernel may hide"
    ]
    assert checks.lists("a", None)[0].level == WARN


def test_ha_findings() -> None:
    assert checks.ha("cluster", parse.parse_ha(HA)) == []
    assert messages(checks.ha("c", parse.parse_ha("lrm ace (active)"))) == [
        "c: ha-manager does not report quorum OK"
    ]
    status = parse.parse_ha(
        "quorum OK\nlrm ace (maintenance mode, x)\nlrm bray (idle)\n"
        "service ct:1 (ace, error)\nservice vm:2 (bray, migrate)\nservice vm:3 (bray, stopped)"
    )
    assert messages(checks.ha("c", status)) == [
        "c: HA LRM ace is maintenance mode",
        "c: HA ct:1 is error on ace",
        "c: HA vm:2 is still settling (migrate on bray)",
    ]


def test_vm_preflight() -> None:
    helm = vm()
    assert [(f.level, f.message) for f in checks.vm_preflight(helm, ["10.0.40.15"], "r")] == [
        (NOTE, "holds VIP 10.0.40.15; it fails over (expected)")
    ]
    assert checks.vips(helm, ["10.0.40.24"]) == []
    assert checks.vips(helm, ["10.0.40.2"]) == []
    assert messages(checks.ci_jobs(vm("riven", ci="2"))) == [
        "riven: 2 CI job(s) running on its runners"
    ]
    assert checks.ci_jobs(vm("riven", ci="")) == []
    held = checks.vm_preflight(
        vm(
            apt=APT_KERNEL.replace(
                "proxmox-kernel-7.0.14-26-pve-signed", "linux-image-6.12.114+deb13-amd64"
            )
        ),
        [],
        "r",
    )
    assert [f.level for f in held] == [STOP]


def test_swarm_peers() -> None:
    assert checks.swarm_peers(vm()) == []
    assert checks.swarm_peers(vm("riven")) == []
    sick = SWARM.replace("neo\tReady\tReachable", "neo\tDown\tUnreachable")
    assert messages(checks.swarm_peers(vm(swarm=sick))) == [
        "helm: swarm manager(s) not healthy: neo (Down, Unreachable)"
    ]
    worker = SWARM + "\nwork\tDown\t\tfalse"
    assert checks.swarm_peers(vm(swarm=worker)) == []
    not_me = SWARM.replace("Reachable\ttrue", "Reachable\tfalse")
    assert checks.swarm_peers(vm(swarm=not_me.replace("neo\tReady", "neo\tDown"))) == []


# --------------------------------------------------------------------------------------
# checks: after the reboot
# --------------------------------------------------------------------------------------


def test_new_kernel_compares_uname_with_installed() -> None:
    assert checks.new_kernel("a", NEW + "\n", parse.Kernel(True, OLD, NEW)) == []
    assert messages(checks.new_kernel("a", OLD, parse.Kernel(True, OLD, NEW))) == [
        f"a: running {OLD}, installed {NEW}"
    ]
    assert messages(checks.new_kernel("a", "", parse.Kernel(True, OLD, NEW))) == [
        f"a: running ?, installed {NEW}"
    ]
    assert (
        "no installed-kernel" in checks.new_kernel("a", NEW, parse.Kernel(None, "", ""))[0].message
    )


def test_scraped() -> None:
    assert checks.scraped("a", [1.0, 1.0], "helm") == []
    assert messages(checks.scraped("a", None, "helm")) == ["a: could not ask helm for up{host}"]
    assert checks.scraped("a", None, "helm")[0].level == STOP
    assert messages(checks.scraped("a", [1.0], None)) == [
        "a: no docker-stacks host runs victoria-metrics: cannot check up"
    ]
    assert checks.scraped("a", [1.0], None)[0].level == STOP
    assert messages(checks.scraped("a", [], "helm")) == [
        "a: no up series: monitoring is not scraping it"
    ]
    assert messages(checks.scraped("a", [1.0, 0.0], "helm")) == [
        "a: up is 0 for at least one scrape job"
    ]


def test_monitor_ready() -> None:
    assert checks.monitor_ready("helm", {"alertmanager": "200", "victoriametrics": "200"}) == []
    found = checks.monitor_ready("helm", {"alertmanager": "503", "victoriametrics": ""})
    assert messages(found) == [
        "helm: alertmanager is not ready (HTTP 503)",
        "helm: victoriametrics is not ready (HTTP ?)",
    ]
    assert {item.level for item in found} == {STOP}


def test_node_recovered() -> None:
    assert checks.node_recovered(node(pending=False)) == []
    assert checks.node_recovered(node("osiris", pending=False, ha="garbage")) == []
    broken = node(
        pending=True,
        pools="pool: x",
        exporter="inactive",
        ha=HA.replace("started)\n", "migrate)\n", 1),
    )
    assert messages(checks.node_recovered(broken)) == [
        f"ace: running {OLD}, installed {NEW}",
        "ace: zpool status -x: pool: x",
        "ace: HA ct:101 is still settling (migrate on ace)",
        "ace: prometheus-node-exporter is inactive",
    ]
    assert "is ?" in checks.node_recovered(node(pending=False, exporter=""))[0].message


def test_osiris_guests_must_be_running_again() -> None:
    def osiris(**overrides: str) -> list[str]:
        return messages(checks.node_recovered(node("osiris", pending=False, **overrides)))

    deepstone_down = [OSIRIS_RESOURCES[0], OSIRIS_RESOURCES[1] | {"status": "stopped"}]
    assert osiris(resources=json.dumps(deepstone_down)) == [
        "osiris: onboot guest(s) not running: 111 deepstone (stopped)"
    ]
    assert osiris(resources=json.dumps(OSIRIS_RESOURCES[:1])) == [
        "osiris: onboot guest(s) not running: 111 (not listed on this node)"
    ]
    assert osiris(resources=json.dumps([{"vmid": 105, "node": "osiris"}])) == [
        "osiris: onboot guest(s) not running: 105 ? (?), 111 (not listed on this node)"
    ]
    assert osiris(resources="") == ["osiris: cannot read guest status from /cluster/resources"]
    assert osiris(onboot="") == ["osiris: cannot list the onboot guests in /etc/pve/local"]
    assert osiris(onboot="listed", resources="") == []


def test_ha_guests_are_left_to_ha() -> None:
    stopped = [r | {"status": "stopped"} for r in RESOURCES]
    assert checks.guests(node("ace", pending=False, resources=json.dumps(stopped))) == []
    # A guest on another node with the same vmid is not this node's guest.
    elsewhere = [{"vmid": 105, "node": "ace", "status": "running"}]
    found = checks.guests(node("osiris", pending=False, resources=json.dumps(elsewhere)))
    assert "105 (not listed on this node)" in found[0].message


def test_node_units_must_be_up() -> None:
    broken = node(
        pending=False,
        failed="pvescheduler.service loaded failed failed PVE scheduler",
        enabled=ENABLED + "\n" + DOWN,
    )
    assert messages(checks.node_recovered(broken)) == [
        "ace: failed unit(s): pvescheduler.service",
        "ace: enabled service(s) not active: pveproxy.service",
    ]
    assert messages(checks.node_recovered(node(pending=False, enabled=""))) == [
        "ace: cannot read the enabled services"
    ]


def test_vm_recovered() -> None:
    assert checks.vm_recovered(vm(pending=False)) == []
    broken = vm(
        pending=False,
        failed="docker.service loaded failed failed Docker",
        userfailed="opencode.service loaded failed failed OpenCode",
        containers="grafana\tRestarting (1) 2 seconds ago",
        swarm=SWARM.replace("helm\tReady\tReachable", "helm\tDown\tUnreachable"),
    )
    assert messages(checks.vm_recovered(broken)) == [
        "helm: failed unit(s): docker.service, opencode.service",
        "helm: container(s) not up: grafana (Restarting (1) 2 seconds ago)",
        "helm: not back in the swarm (Down, Unreachable)",
    ]
    assert messages(checks.vm_recovered(vm(pending=False, enabled=""))) == [
        "helm: cannot read the enabled services"
    ]


INACTIVE_OPENCODE = USER_ENABLED.replace(
    "Id=opencode.service\nTriggeredBy=\nActiveState=active",
    "Id=opencode.service\nTriggeredBy=\nActiveState=inactive",
)


def test_riven_required_units_are_its_enabled_ones() -> None:
    assert checks.vm_recovered(vm("riven", pending=False)) == []
    enabled = ENABLED + "\n" + DOWN
    inactive = vm("riven", pending=False, userenabled=INACTIVE_OPENCODE, enabled=enabled)
    assert messages(checks.vm_recovered(inactive)) == [
        "riven: enabled service(s) not active: pveproxy.service, opencode.service"
    ]
    # An inactive unit is not failed: the old failed-only check let this through.
    assert inactive.failed_units == []
    # Without linger the user's units are session-bound: not required after a boot.
    unlingered = vm("riven", pending=False, userenabled=INACTIVE_OPENCODE, linger="no")
    assert checks.vm_recovered(unlingered) == []
    assert unlingered.user_manager is None
    # A lingering user with nothing enabled is not a failed reading.
    assert checks.vm_recovered(vm("riven", pending=False, userenabled="")) == []
    assert checks.vm_recovered(vm("riven", pending=False, usermanager="degraded")) == []


@pytest.mark.parametrize(("state", "shown"), [("offline", "offline"), ("", "?")])
def test_a_lingering_user_manager_must_run(state: str, shown: str) -> None:
    found = checks.vm_recovered(vm("riven", pending=False, usermanager=state))
    assert messages(found) == [
        f"riven: the lingering user manager is {shown}: its units are not up"
    ]


# --------------------------------------------------------------------------------------
# plan: wave order
# --------------------------------------------------------------------------------------

CLUSTER_NODES = ["ace", "bray", "clovis"]


@pytest.mark.parametrize(
    ("tower", "expected"),
    [
        # The runbook's table: tower on ace, tower on clovis, tower on bray.
        ("ace", [["osiris", "clovis"], ["ace"], ["bray"]]),
        ("clovis", [["osiris", "ace"], ["clovis"], ["bray"]]),
        ("bray", [["osiris", "ace"], ["clovis"], ["bray"]]),
        (None, [["osiris", "ace"], ["clovis"], ["bray"]]),
    ],
)
def test_wave_order_follows_tower(tower: str | None, expected: list[list[str]]) -> None:
    assert plan.order_waves(CLUSTER_NODES, ["osiris"], tower, "bray") == expected


def test_controller_node_goes_last_wherever_it_is() -> None:
    assert plan.order_waves(CLUSTER_NODES, ["osiris"], "clovis", "ace") == [
        ["osiris", "bray"],
        ["clovis"],
        ["ace"],
    ]
    assert plan.order_waves(CLUSTER_NODES, ["osiris"], "ace", None) == [
        ["osiris", "bray"],
        ["clovis"],
        ["ace"],
    ]
    assert plan.order_waves(CLUSTER_NODES, [], "bray", "bray") == [["ace"], ["clovis"], ["bray"]]
    assert plan.order_waves([], ["osiris"], None, None) == [["osiris"]]
    assert plan.order_waves([], [], None, None) == []


def test_never_two_cluster_nodes_in_one_wave() -> None:
    for tower in [*CLUSTER_NODES, None]:
        for controller in [*CLUSTER_NODES, None]:
            waves = plan.order_waves(CLUSTER_NODES, ["osiris"], tower, controller)
            assert all(sum(n in CLUSTER_NODES for n in wave) <= 1 for wave in waves)
            assert sorted(n for wave in waves for n in wave) == sorted([*CLUSTER_NODES, "osiris"])


def test_keep_drops_emptied_waves() -> None:
    waves = [["osiris", "clovis"], ["ace"], ["bray"]]
    assert plan.keep(waves, {"clovis", "bray"}) == [["clovis"], ["bray"]]
    assert plan.keep(waves, {"osiris"}) == [["osiris"]]
    assert plan.keep(waves, set()) == []


def test_vm_order_puts_the_controller_last() -> None:
    assert plan.order_vms(["riven", "helm", "shaxx"], "riven") == ["helm", "shaxx", "riven"]
    assert plan.order_vms(["helm"], "riven") == ["helm"]


def test_scope() -> None:
    assert SCOPE == ["ace", "bray", "clovis", "osiris", "riven", "helm", "shaxx"]
    assert plan.scoped(INVENTORY, ["helm", "ace"]) == ["ace", "helm"]
    with pytest.raises(ValueError, match="not a PVE node or an own-kernel VM: tower, zion"):
        plan.scoped(INVENTORY, ["zion", "tower", "ace"])


def test_remedy_by_host() -> None:
    assert plan.remedy(INVENTORY, "ace") == "./deploy --confirm-upgrade pve-upgrade ace"
    assert plan.remedy(INVENTORY, "helm") == (
        "systemctl start homelab-apt-dist-upgrade.service on helm, as root"
    )


# --------------------------------------------------------------------------------------
# plan: the whole survey
# --------------------------------------------------------------------------------------


def test_nothing_pending_is_the_expected_week() -> None:
    result = make_plan(Cluster())
    assert result.verdict == "nothing-pending"
    assert result.waves == [] and result.vms == [] and result.stops == []
    assert result.current == ["ace", "bray", "clovis", "osiris", "helm", "riven"]
    # shaxx is declared in the inventory but has no vmid on the cluster yet.
    assert result.not_built == ["shaxx"]


def test_full_roll() -> None:
    result = make_plan(Cluster(pending=("ace", "bray", "clovis", "osiris", "helm", "riven")))
    assert result.verdict == "ready"
    assert [[step.host for step in wave] for wave in result.waves] == [
        ["osiris", "clovis"],
        ["ace"],
        ["bray"],
    ]
    osiris, clovis = result.waves[0]
    assert (osiris.running, osiris.installed) == (OLD, NEW)
    assert osiris.details == [
        "xur (ct:105, no HA: down with the node until it boots)",
        "deepstone (ct:111, no HA: down with the node until it boots)",
    ]
    assert clovis.details == ["helm (vm:112, HA: live-migrates)"]
    assert result.waves[1][0].details == [
        "tower (ct:101, HA: LXC restarts on another node)",
        "zion (vm:109, HA: live-migrates)",
    ]
    assert [step.host for step in result.vms] == ["helm", "riven"]
    assert result.vms[0].details == ["holds VIP 10.0.40.15; it fails over (expected)"]
    assert result.warnings == [] and result.held == []


def test_only_pending_nodes_are_waved() -> None:
    result = make_plan(Cluster(pending=("bray", "osiris")))
    assert [[step.host for step in wave] for wave in result.waves] == [["osiris"], ["bray"]]
    assert result.current == ["ace", "clovis", "helm", "riven"]


def test_a_subset_keeps_roll_order_and_still_reads_tower() -> None:
    cluster = Cluster(pending=("ace", "clovis"))
    result = make_plan(cluster, scope=plan.scoped(INVENTORY, ["ace", "clovis"]))
    assert [[step.host for step in wave] for wave in result.waves] == [["clovis"], ["ace"]]
    # Cluster facts come from every node, even outside the subset.
    assert {host for host, _ in cluster.calls} >= {"ace", "bray", "clovis", "osiris"}


def test_a_stop_on_a_pending_node_blocks_the_roll() -> None:
    cluster = Cluster(pending=("ace", "bray"))
    cluster.hosts["bray"] = node_readings("bray", True, pools="  pool: rpool\n state: DEGRADED")
    result = make_plan(cluster)
    assert result.verdict == "stop"
    assert messages(result.stops) == ["bray: zpool status -x: pool: rpool"]
    assert "Do not reboot anything" in "\n".join(plan.render(result))


def test_a_stop_on_a_current_node_only_warns() -> None:
    cluster = Cluster(pending=("ace",))
    cluster.hosts["clovis"] = node_readings("clovis", False, apt=APT_KERNEL)
    result = make_plan(cluster)
    assert result.verdict == "ready"
    assert [f.level for f in result.warnings] == [WARN]
    assert result.warnings[0].message.startswith("kernel 7.0.14-26-pve is available")
    assert result.warnings[0].message.endswith("(not pending)")


def test_newer_kernel_on_a_pending_node_stops_with_its_remedy() -> None:
    cluster = Cluster(pending=("clovis",))
    cluster.hosts["clovis"] = node_readings("clovis", True, apt=APT_KERNEL)
    result = make_plan(cluster)
    assert result.verdict == "stop"
    assert "./deploy --confirm-upgrade pve-upgrade clovis" in result.stops[0].message


def test_unknown_reboot_state_stops_without_being_waved() -> None:
    cluster = Cluster(pending=("ace",))
    cluster.hosts["bray"] = node_readings("bray", kernel="")
    result = make_plan(cluster)
    assert messages(result.stops) == ["bray: the reboot exporter has no reading (reboot.prom)"]
    assert [[step.host for step in wave] for wave in result.waves] == [["ace"]]
    assert "bray" not in result.current


def test_unreachable_node_in_scope_stops() -> None:
    cluster = Cluster(pending=("ace",), osiris=None)
    result = make_plan(cluster)
    assert messages(result.stops) == ["osiris: unreachable: osiris: no route to host"]


def test_unreachable_node_outside_scope_is_ignored() -> None:
    cluster = Cluster(pending=("ace",), osiris=None)
    result = make_plan(cluster, scope=["ace"])
    assert result.verdict == "ready"


def test_no_cluster_view_stops() -> None:
    cluster = Cluster(ace=None, bray=None, clovis=None)
    result = make_plan(cluster, scope=["osiris"])
    assert messages(result.stops) == ["cluster: no cluster node answered"]


def test_running_pbs_job_blocks_its_node() -> None:
    cluster = Cluster(pending=("osiris",))
    cluster.pbs = json.dumps([{"worker_type": "backup", "worker_id": "vm/110"}])
    result = make_plan(cluster)
    assert messages(result.stops) == ["osiris: task(s) running on xur: backup vm/110"]


def test_unsettled_ha_blocks_only_when_something_is_pending() -> None:
    unsettled = HA.replace("vm:112 (clovis, started)", "vm:112 (clovis, migrate)")
    quiet = Cluster(**{n: node_readings(n, ha=unsettled) for n in ("ace", "bray", "clovis")})
    assert make_plan(quiet).verdict == "nothing-pending"
    busy = Cluster(
        pending=("ace",),
        **{n: node_readings(n, n == "ace", ha=unsettled) for n in ("ace", "bray", "clovis")},
    )
    result = make_plan(busy)
    assert messages(result.stops) == ["cluster: HA vm:112 is still settling (migrate on clovis)"]


def test_window_findings_join_only_a_real_roll() -> None:
    window = [Finding(STOP, "schedule", "inside the window"), Finding(WARN, "schedule", "soon")]
    assert make_plan(Cluster(), window=window).verdict == "nothing-pending"
    result = make_plan(Cluster(pending=("helm",)), window=window)
    assert messages(result.stops) == ["schedule: inside the window"]
    assert messages(result.warnings) == ["schedule: soon"]


def test_vm_with_a_ci_job_is_held_and_the_roll_goes_on() -> None:
    cluster = Cluster(pending=("ace", "riven"))
    cluster.hosts["riven"] = vm_readings("riven", True, ci="1")
    result = make_plan(cluster)
    assert result.verdict == "ready"
    assert messages(result.held) == ["riven: 1 CI job(s) running on its runners"]
    assert result.vms == []


def test_vm_warnings_and_unreachable_vms() -> None:
    cluster = Cluster(pending=("helm",), riven=None)
    cluster.hosts["helm"] = vm_readings(
        "helm", True, apt="1 upgraded, 0 newly installed, 0 to remove"
    )
    result = make_plan(cluster)
    assert [step.host for step in result.vms] == ["helm"]
    assert messages(result.warnings) == ["helm: 1 non-kernel package change(s) available"]
    assert messages(result.held) == ["riven: unreachable: riven: no route to host"]


def test_current_vm_problems_only_warn() -> None:
    cluster = Cluster()
    cluster.hosts["riven"] = vm_readings("riven", False, ci="1")
    result = make_plan(cluster)
    assert messages(result.warnings) == ["riven: 1 CI job(s) running on its runners (not pending)"]


def test_vm_existence_is_assumed_without_a_cluster_view() -> None:
    assert plan.vm_exists(INVENTORY, "shaxx", None)
    assert not plan.vm_exists(INVENTORY, "shaxx", RESOURCES)
    assert plan.vm_exists(INVENTORY, "helm", RESOURCES)


def test_plan_vm_without_facts_is_held() -> None:
    result = plan.Plan()
    plan.plan_vm(result, INVENTORY, "helm", None)
    assert messages(result.held) == ["helm: unreachable: not surveyed"]


def test_render_and_json() -> None:
    result = make_plan(Cluster(pending=("ace", "helm")))
    result.warnings.append(Finding(WARN, "x", "careful"))
    lines = plan.render(result)
    assert lines[:4] == ["verdict: ready", "", "Wave 1: ace", f"  ace      {OLD} -> {NEW}"]
    assert "VMs, after every node wave (reboot from inside the guest):" in lines
    assert "  x: careful" in lines
    assert "Nothing pending: bray, clovis, osiris, riven" in lines
    assert lines[-1] == "Not built yet: shaxx"
    data = result.as_dict()
    assert data["verdict"] == "ready"
    assert data["waves"][0][0]["host"] == "ace"
    json.dumps(data)


def test_step_lines_mark_unknown_kernels() -> None:
    assert plan.step_lines(plan.Step("a", "", "", [])) == ["  a        ? -> ?"]


# --------------------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------------------

TEMPLATE = """
route: {}
time_intervals:
  - name: other
    time_intervals: []
  - name: scheduled-maintenance
    time_intervals:
      - times:
          - start_time: "02:00"
            end_time: "02:10"
        location: America/New_York
      - times:
          - start_time: "08:00"
            end_time: "08:10"
        location: America/New_York
"""


def test_windows_come_from_the_alertmanager_template() -> None:
    parsed = windows.parse_windows(TEMPLATE)
    assert parsed == [
        windows.Window(time(2, 0), time(2, 10), "America/New_York"),
        windows.Window(time(8, 0), time(8, 10), "America/New_York"),
    ]
    assert windows.parse_windows("time_intervals: []") == []
    assert windows.parse_windows("") == []
    real = windows.load_windows(ROOT)
    assert [(w.start, w.end) for w in real] == [
        (time(2, 0), time(2, 10)),
        (time(8, 0), time(8, 10)),
    ]
    assert windows.load_windows(ROOT / "nowhere") == []


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # 2026-10-21 is EDT, UTC-4.
        (
            "2026-10-21T12:05:00",
            [(STOP, "inside the 08:00-08:10 America/New_York maintenance window")],
        ),
        ("2026-10-21T12:10:00", []),
        (
            "2026-10-21T11:15:00",
            [(WARN, "the 08:00-08:10 America/New_York maintenance window opens in 45 min")],
        ),
        (
            "2026-10-21T11:00:00",
            [(WARN, "the 08:00-08:10 America/New_York maintenance window opens in 60 min")],
        ),
        ("2026-10-21T10:59:00", []),
        ("2026-10-21T01:44:00", []),
    ],
)
def test_window_findings(utc: str, expected: list[tuple[str, str]]) -> None:
    now = datetime.fromisoformat(utc).replace(tzinfo=UTC)
    found = windows.window_findings(windows.parse_windows(TEMPLATE), now)
    assert [(f.level, f.message) for f in found] == expected


def test_no_windows_warns() -> None:
    (found,) = windows.window_findings([], datetime.now(UTC))
    assert found.level == WARN


# --------------------------------------------------------------------------------------
# survey: inventory, scripts, SSH
# --------------------------------------------------------------------------------------

HOSTS_CONF = """
ace: {config: {type: pve, hostname: ace.x.internal, user: root, sshkey: infra}}
osiris:
  config: {type: pve, hostname: osiris.x.internal, user: root, sshkey: infra, standalone: true}
  features: {pve-upgrade: }
xur:
  config: {type: pbs, hostname: xur.x.internal, user: root, sshkey: infra}
  features: {pve-guest: {vmid: 105, kind: lxc, home_node: osiris}}
helm:
  config:
    type: ubuntu
    hostname: helm.x.internal
    user: root
    sshkey: infra
    ssh_config: {user: freender}
  features:
    pve-guest: {vmid: 112, kind: qemu, home_node: ace}
    metrics-exporters: {own_kernel: true}
    keepalived: {virtual_ips: ["10.0.40.15 dev nic0"]}
    docker-stacks: {stacks: [grafana, victoria-metrics]}
zion:
  config: {type: haos, hostname: zion.x.internal, user: root, sshkey: homelab}
  features: {pve-guest: {vmid: 109, kind: qemu, home_node: ace}}
tower:
  config: {type: ubuntu, hostname: tower.x.internal, user: root, sshkey: infra}
  features:
    pve-guest: {vmid: 101, kind: lxc, home_node: ace}
    docker-stacks: {stacks: [traefik]}
"""


def registry(tmp_path: Path, text: str = HOSTS_CONF) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(text, encoding="utf-8")
    return HostRegistry(path)


def test_inventory_from_hosts_conf(tmp_path: Path) -> None:
    inventory = survey.load_inventory(registry(tmp_path))
    assert inventory.nodes == {"ace": False, "osiris": True}
    assert inventory.vms == ["helm"]
    assert inventory.vmids == {"xur": 105, "helm": 112, "zion": 109, "tower": 101}
    assert inventory.pbs == ["xur"]
    assert inventory.vips == {"helm": ["10.0.40.15"]}
    assert inventory.users == {"helm": "freender"}
    assert inventory.monitor == "helm"
    assert inventory.upgraders == ["osiris"]


def test_inventory_without_a_metrics_host(tmp_path: Path) -> None:
    text = HOSTS_CONF.replace("grafana, victoria-metrics", "grafana")
    assert survey.load_inventory(registry(tmp_path, text)).monitor is None


def test_real_inventory_covers_every_node_and_own_kernel_vm() -> None:
    inventory = survey.load_inventory(HostRegistry(ROOT / "hosts.conf"))
    assert inventory.nodes == {"ace": False, "bray": False, "clovis": False, "osiris": True}
    assert {"riven", "helm"} <= set(inventory.vms)
    assert "zion" not in inventory.vms
    assert {plan.TOWER, plan.CONTROLLER} <= set(inventory.vmids)
    assert inventory.monitor == "helm"


def test_scripts_print_every_section_and_always_exit_zero() -> None:
    node_text = survey.node_script("ace")
    names = [line.split("@@%s\\n' ")[1].split(";")[0] for line in node_text.splitlines()[:-1]]
    assert names == list(node_readings("ace"))
    assert node_text.endswith("\ntrue")
    assert "/nodes/ace/tasks --source active" in node_text
    vm_names = [
        line.split("@@%s\\n' ")[1].split(";")[0]
        for line in survey.vm_script(None).splitlines()[:-1]
    ]
    assert set(vm_names) == set(vm_readings("helm"))
    user_names = [
        line.split("@@%s\\n' ")[1].split(";")[0]
        for line in survey.vm_script("freender").splitlines()[:-1]
    ]
    assert set(user_names) == set(vm_readings("riven"))
    assert "--user -M freender@ show" in survey.vm_script("freender")
    assert "loginctl show-user freender -p Linger" in survey.vm_script("freender")
    assert "test -d /etc/pve/local && echo listed" in node_text
    assert "Runner[.]Worker" in survey.vm_script(None)


def test_up_command_quotes_the_selector() -> None:
    assert survey.up_command("ace").endswith("--data-urlencode 'query=up{host=\"ace\"}'")


class FakeResult:
    def __init__(self, exited: int, stdout: str = "") -> None:
        self.exited = exited
        self.stdout = stdout


class FakeConnection:
    def __init__(self, results: list) -> None:
        self.results = results
        self.connect_timeout = None
        self.kwargs: list[dict] = []

    def run(self, command: str, **kwargs):
        self.kwargs.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_ssh_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[FakeConnection] = []
    queue = [
        [FakeResult(0, "out"), FakeResult(3), OSError("Connection refused")],
        [FakeResult(0, "again")],
    ]

    def connect(root, host):
        conn = FakeConnection(queue.pop(0))
        made.append(conn)
        return type("HostConnection", (), {"connection": conn})()

    monkeypatch.setattr(survey, "connection_for_host", connect)
    run = survey.ssh_runner(ROOT)
    assert run("ace", "x") == "out"
    with pytest.raises(survey.HostDown, match="survey script exited 3"):
        run("ace", "x")
    with pytest.raises(survey.HostDown, match="OSError: Connection refused"):
        run("ace", "x")
    # A failed connection is dropped, so the next call reconnects (after a reboot).
    assert run("ace", "x") == "again"
    assert len(made) == 2
    assert made[0].connect_timeout == survey.CONNECT_TIMEOUT
    assert made[0].kwargs[0] == {"hide": True, "warn": True, "timeout": survey.COMMAND_TIMEOUT}


def test_pbs_tasks_survives_a_down_pbs() -> None:
    cluster = Cluster()
    assert survey.pbs_tasks(cluster, "xur") == []

    def down(host: str, command: str) -> str:
        raise survey.HostDown("x")

    assert survey.pbs_tasks(down, "xur") is None


# --------------------------------------------------------------------------------------
# run_plan / run_verify / CLI
# --------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


@pytest.fixture
def fake_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "load_inventory", lambda registry: INVENTORY)


@pytest.mark.usefixtures("fake_inventory")
def test_run_plan_text_and_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert survey.run_plan(ROOT, [], False, runner=Cluster(pending=("ace",)), now=NOW) == 0
    assert capsys.readouterr().out.startswith("verdict: ready\n")
    stopping = Cluster(pending=("ace",))
    stopping.hosts["ace"] = node_readings("ace", True, tasks='[{"type": "vzdump", "id": "101"}]')
    assert survey.run_plan(ROOT, [], False, runner=stopping, now=NOW) == 1
    assert "ace: task(s) running: vzdump 101" in capsys.readouterr().out


@pytest.mark.usefixtures("fake_inventory")
def test_run_plan_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert survey.run_plan(ROOT, ["helm"], True, runner=Cluster(), now=NOW) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "nothing-pending"
    assert data["current"] == ["helm"]


@pytest.mark.usefixtures("fake_inventory")
def test_run_plan_refuses_inside_a_window(capsys: pytest.CaptureFixture[str]) -> None:
    inside = datetime(2026, 10, 21, 12, 5, tzinfo=UTC)
    assert survey.run_plan(ROOT, [], False, runner=Cluster(pending=("ace",)), now=inside) == 1
    assert "maintenance window" in capsys.readouterr().out


@pytest.mark.usefixtures("fake_inventory")
def test_run_verify_passes_on_recovered_hosts(capsys: pytest.CaptureFixture[str]) -> None:
    code = survey.run_verify(ROOT, ["ace", "helm"], 0, False, runner_factory=Cluster)
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"ace: ok ({NEW}, up 3636 min)",
        f"helm: ok ({VM_NEW}, up 10 min)",
    ]


@pytest.mark.usefixtures("fake_inventory")
def test_run_verify_waits_for_a_host_to_come_back(capsys: pytest.CaptureFixture[str]) -> None:
    passes = [Cluster(ace=None), Cluster(pending=("ace",)), Cluster()]
    clock = iter([0, 10, 20, 30])
    slept: list[float] = []
    code = survey.run_verify(
        ROOT,
        ["ace"],
        60,
        False,
        runner_factory=lambda: passes.pop(0),
        sleep=slept.append,
        clock=lambda: next(clock),
    )
    assert code == 0
    assert slept == [survey.VERIFY_INTERVAL, survey.VERIFY_INTERVAL]
    captured = capsys.readouterr()
    assert captured.err.count("waiting on ace") == 2
    assert captured.out.splitlines() == [f"ace: ok ({NEW}, up 3636 min)", "helm: ok (monitoring)"]


@pytest.mark.usefixtures("fake_inventory")
def test_run_verify_gives_up_at_the_deadline(capsys: pytest.CaptureFixture[str]) -> None:
    clock = iter([0, 100])
    code = survey.run_verify(
        ROOT,
        ["ace"],
        60,
        True,
        runner_factory=lambda: Cluster(pending=("ace",)),
        sleep=lambda s: None,
        clock=lambda: next(clock),
    )
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["hosts"]["ace"]["findings"][0] == {
        "level": "stop",
        "host": "ace",
        "message": f"running {OLD}, installed {NEW}",
    }


@pytest.mark.usefixtures("fake_inventory")
def test_verify_reports_unreachable_and_unscraped(capsys: pytest.CaptureFixture[str]) -> None:
    cluster = Cluster(osiris=None)
    cluster.up = {"data": {"result": [{"value": [0, "0"]}]}}
    assert (
        survey.run_verify(ROOT, ["osiris", "riven"], 0, False, runner_factory=lambda: cluster) == 1
    )
    assert capsys.readouterr().out.splitlines() == [
        "osiris: FAIL (unreachable)",
        "  stop: unreachable: osiris: no route to host",
        f"riven: FAIL ({VM_NEW}, up 10 min)",
        "  stop: up is 0 for at least one scrape job",
        "helm: ok (monitoring)",
    ]


@pytest.mark.usefixtures("fake_inventory")
def test_verify_waits_for_osiris_guests_and_riven_units(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stopped = [OSIRIS_RESOURCES[0] | {"status": "stopped"}, OSIRIS_RESOURCES[1]]
    cluster = Cluster(
        osiris=node_readings("osiris", resources=json.dumps(stopped)),
        riven=vm_readings("riven", userenabled=INACTIVE_OPENCODE),
    )
    code = survey.run_verify(ROOT, ["osiris", "riven"], 0, False, runner_factory=lambda: cluster)
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        f"osiris: FAIL ({NEW}, up 3636 min)",
        "  stop: onboot guest(s) not running: 105 xur (stopped)",
        f"riven: FAIL ({VM_NEW}, up 10 min)",
        "  stop: enabled service(s) not active: opencode.service",
        "helm: ok (monitoring)",
    ]


@pytest.mark.usefixtures("fake_inventory")
def test_verify_fails_a_node_wave_when_the_monitor_is_down(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = survey.run_verify(ROOT, ["ace"], 0, False, runner_factory=lambda: Cluster(helm=None))
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        f"ace: FAIL ({NEW}, up 3636 min)",
        "  stop: could not ask helm for up{host}",
        "helm: FAIL (monitoring)",
        "  stop: monitoring is unreachable: helm: no route to host",
    ]


@pytest.mark.usefixtures("fake_inventory")
def test_verify_fails_when_alertmanager_is_not_ready(capsys: pytest.CaptureFixture[str]) -> None:
    cluster = Cluster()
    cluster.ready = {"alertmanager": "503", "victoriametrics": "200"}
    assert survey.run_verify(ROOT, ["ace"], 0, False, runner_factory=lambda: cluster) == 1
    assert capsys.readouterr().out.splitlines()[1:] == [
        "helm: FAIL (monitoring)",
        "  stop: alertmanager is not ready (HTTP 503)",
    ]


def test_verify_once_folds_readiness_into_a_monitor_target() -> None:
    cluster = Cluster()
    cluster.ready = {"alertmanager": "200", "victoriametrics": "000"}
    state, findings = survey.verify_once(cluster, INVENTORY, ["helm"])["helm"]
    assert state == f"{VM_NEW}, up 10 min"
    assert messages(findings) == ["helm: victoriametrics is not ready (HTTP 000)"]
    # An unreachable monitor target already stops; readiness is not asked twice.
    down = survey.verify_once(Cluster(helm=None), INVENTORY, ["helm"])["helm"]
    assert down[0] == "unreachable"
    assert messages(down[1]) == ["helm: unreachable: helm: no route to host"]


def test_verify_once_without_a_monitor_skips_readiness() -> None:
    no_monitor = plan.Inventory(nodes=INVENTORY.nodes, vms=[], vmids={})
    results = survey.verify_once(Cluster(), no_monitor, ["ace"])
    assert list(results) == ["ace"]
    assert messages(results["ace"][1]) == [
        "ace: no docker-stacks host runs victoria-metrics: cannot check up"
    ]


def test_ready_command_reads_status_codes_only() -> None:
    command = survey.ready_command()
    assert "-o /dev/null -w '%{http_code}'" in command
    assert "localhost:9093/-/ready" in command
    assert "localhost:8428/health" in command
    assert command.endswith("\ntrue")


def test_vm_script_lists_stopped_containers() -> None:
    assert "docker ps -a --format" in survey.vm_script(None)


def test_unhealthy_containers_flags_exited_and_created() -> None:
    assert parse.unhealthy_containers(
        "victoria-metrics\tExited (137) 2 minutes ago\nalertmanager\tCreated\ngrafana\tUp 1 hour"
    ) == ["victoria-metrics (Exited (137) 2 minutes ago)", "alertmanager (Created)"]


def test_scrape_state_without_a_monitor_or_with_it_down() -> None:
    no_monitor = plan.Inventory(nodes={}, vms=[], vmids={})
    assert STOP == survey.scrape_state(Cluster(), no_monitor, "ace")[0].level
    down = Cluster(helm=None)

    def runner(host: str, command: str) -> str:
        if host == "helm":
            raise survey.HostDown("down")
        return down(host, command)

    assert survey.scrape_state(runner, INVENTORY, "ace")[0].message == (
        "could not ask helm for up{host}"
    )


def test_summary_without_uptime() -> None:
    assert survey.summary(vm(uptime="", uname="")) == "?, uptime ?"


def test_cli_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple] = []

    def fake_plan(root, hosts, as_json):
        seen.append(("plan", hosts, as_json))
        return 1

    def fake_verify(root, hosts, wait, as_json):
        seen.append(("verify", hosts, wait, as_json))
        return 0

    monkeypatch.setattr(survey, "run_plan", fake_plan)
    monkeypatch.setattr(survey, "run_verify", fake_verify)
    runner = CliRunner()
    assert runner.invoke(cli.main, ["roll", "plan", "--json", "ace"]).exit_code == 1
    assert runner.invoke(cli.main, ["roll", "verify", "--wait", "30", "ace"]).exit_code == 0
    assert runner.invoke(cli.main, ["roll", "verify"]).exit_code == 2
    assert seen == [("plan", ["ace"], True), ("verify", ["ace"], 30, False)]


def test_cli_turns_a_bad_host_into_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args):
        raise ValueError("not a PVE node or an own-kernel VM: zion")

    monkeypatch.setattr(survey, "run_plan", refuse)
    monkeypatch.setattr(survey, "run_verify", refuse)
    runner = CliRunner()
    for args in (["roll", "plan", "zion"], ["roll", "verify", "zion"]):
        result = runner.invoke(cli.main, args)
        assert result.exit_code == 1
        assert "not a PVE node or an own-kernel VM: zion" in result.output


def test_wrapper_dispatches_to_roll() -> None:
    wrapper = (ROOT / "roll").read_text(encoding="utf-8")
    assert wrapper.count("-m homelab.cli roll") == 3
    assert (ROOT / "roll").stat().st_mode & 0o111
