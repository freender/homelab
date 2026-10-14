"""pve-guest: schema validation, Proxmox parsing, drift labels, and the live report.

The fixtures under tests/fixtures/pve_guest/ are `pvesh ... --output-format json`
reads of the real cluster and of osiris (2026-10-13, identity values and runtime
stats removed). `test_every_declared_guest_is_in_sync_with_the_fixture` is the golden
check: the seeded hosts.conf blocks must describe those reads exactly, so a spec
edit that is not also a real change shows up here first.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from homelab.hosts import HostRegistry, default_registry
from homelab.modules import pve_guest
from homelab.modules.pve_guest import drift, live, pveconf
from homelab.modules.pve_guest.inventory import (
    check_vmid_references,
    declared_guests,
    load_guests,
    pve_nodes,
    validate_inventory,
)
from homelab.modules.pve_guest.spec import (
    DiskSpec,
    HaSpec,
    NetSpec,
    ReplicationSpec,
    parse_guest,
    to_pve,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pve_guest"
NODES = {"ace": False, "bray": False, "clovis": False, "osiris": True}


# --------------------------------------------------------------------------------------
# fixture runner: answers pvesh reads from the captured JSON
# --------------------------------------------------------------------------------------


class FixtureRunner:
    def __init__(self, down: tuple[str, ...] = ()) -> None:
        self.down = set(down)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, node: str, command: str) -> str:
        self.calls.append((node, command))
        if node in self.down:
            raise live.NodeUnreachable(f"{node}: connection refused")
        site = FIXTURES / ("osiris" if node == "osiris" else "cluster")
        return (site / self._file(command)).read_text(encoding="utf-8")

    @staticmethod
    def _file(command: str) -> str:
        names = {
            live.RESOURCES: "resources.json",
            live.HA_RESOURCES: "ha_resources.json",
            live.HA_RULES: "ha_rules.json",
            live.REPLICATION: "replication.json",
        }
        if command in names:
            return names[command]
        vmid = command.split("/")[4]
        return f"pending-{vmid}.json"


def fixture_live(site: str, vmid: int) -> dict[str, str]:
    entries = json.loads((FIXTURES / site / f"pending-{vmid}.json").read_text())
    return pveconf.parse_pending(entries)[0]


def real_specs():
    return load_guests(default_registry(ROOT))


# --------------------------------------------------------------------------------------
# golden: hosts.conf against the captured cluster
# --------------------------------------------------------------------------------------


def test_every_declared_guest_is_in_sync_with_the_fixture() -> None:
    specs = real_specs()
    assert len(specs) == 8, "the eight managed guests (void is deferred, #148)"
    runner = FixtureRunner()
    views: dict = {}
    for spec in specs.values():
        key = live.view_key(spec, NODES)
        if key not in views:
            views[key] = live.read_cluster(runner, live.query_nodes(spec, NODES))
        report = live.guest_report(spec, views[key], runner)
        assert report == [f"{spec.kind} {spec.vmid} on {spec.home_node}, running: in sync"]


def test_hand_written_vmids_all_resolve() -> None:
    assert validate_inventory(default_registry(ROOT)) == (8, 21)


def test_every_impact_label_on_a_changed_helm_spec() -> None:
    """Golden drift for one edited spec: each row, and the label it carries."""
    registry = default_registry(ROOT)
    raw = copy.deepcopy(registry.get("helm", "pve-guest"))
    raw["memory"] = 8192  # needs reboot
    raw["balloon"] = 4096  # applies live
    raw["disks"]["scsi0"]["size"] = "40G"  # grow: applies live
    raw["disks"]["scsi1"]["size"] = "32G"  # shrink: refused
    raw["disks"]["scsi1"]["backup"] = True  # config only
    raw["disks"]["scsi3"] = {"role": "data", "size": "10G", "mount": "/mnt/data"}  # new disk
    raw["net"]["net0"]["tag"] = 10  # applies live
    raw["net"]["net1"] = {"name": "nic1", "mac": "BC:24:11:00:00:01"}
    raw["ha"] = {"nodes": {"clovis": 3, "ace": 1}, "strict": False}
    raw["replication"]["schedule"] = "*/5"
    raw["extra"] = {"startup": "order=5"}
    spec = parse_guest("helm", raw, NODES)

    view = live.read_cluster(FixtureRunner(), ["clovis"])
    current = fixture_live("cluster", 112)
    current["vga"] = "serial0"
    rows = drift.guest_drift(spec, current, view.ha["vm:112"], view.replication[112])

    assert [(r.key, r.want, r.have, r.impact) for r in rows] == [
        ("memory", "8192", "6144", drift.REBOOT),
        ("balloon", "4096", "6144", drift.LIVE),
        ("startup", "order=5", "-", drift.LIVE),
        ("scsi0.size", "40G", "32G", drift.LIVE),
        ("scsi1.size", "32G", "64G", "refused (shrink)"),
        ("scsi1.backup", "yes", "no", drift.CONFIG),
        ("scsi3", "new data disk", "-", drift.LIVE),
        ("net0.tag", "10", "40", drift.LIVE),
        ("net1", "new nic1 BC:24:11:00:00:01", "-", drift.LIVE),
        ("vga", "-", "serial0", drift.UNMANAGED),
        ("ha.nodes", "ace:1,clovis:3", "ace:2,clovis:3", drift.CLUSTER),
        ("ha.strict", "no", "yes", drift.CLUSTER),
        ("replication.schedule", "*/5", "*/15", drift.CLUSTER),
    ]


def test_a_shared_ha_rule_says_the_edit_splits_it() -> None:
    raw = copy.deepcopy(default_registry(ROOT).get("riven", "pve-guest"))
    raw["ha"]["nodes"] = {"bray": 3, "clovis": 2}
    raw["replication"]["target"] = "clovis"
    spec = parse_guest("riven", raw, NODES)
    view = live.read_cluster(FixtureRunner(), ["bray"])

    (row,) = drift.ha_diffs(spec, view.ha["vm:110"])

    assert row.key == "ha.nodes"
    assert row.impact == (
        "cluster config (splits rule ha-rule-7025fad3-d42b, shared with ct:104, ct:108)"
    )


def test_lxc_scalar_labels_and_unmanaged_defaults() -> None:
    raw = copy.deepcopy(default_registry(ROOT).get("neo", "pve-guest"))
    raw["unprivileged"] = False
    raw["features"] = "nesting=1"
    raw["cores"] = 4
    raw["net"]["net0"]["ip"] = "10.0.40.18/24"
    raw["disks"]["mp0"]["mount"] = "/srv/appdata"
    raw["disks"]["mp6"] = {"role": "scratch"}
    spec = parse_guest("neo", raw, NODES)
    current = fixture_live("cluster", 108)
    del current["swap"]  # a missing key compares as the PVE default (512)

    keys = {r.key: r.impact for r in drift.guest_drift(spec, current, None, [])}

    assert keys["unprivileged"] == drift.REFUSED
    assert keys["features"] == drift.REBOOT
    assert keys["cores"] == drift.LIVE
    assert keys["swap"] == drift.LIVE
    assert keys["net0.ip"] == drift.LIVE
    assert keys["mp0.mount"] == drift.REBOOT
    assert keys["mp6"] == drift.REBOOT
    assert keys["ha"] == drift.CLUSTER
    assert keys["replication"] == drift.CLUSTER


def test_protection_drift_applies_live_and_is_unmanaged_when_undeclared() -> None:
    xur = real_specs()["xur"]
    current = fixture_live("osiris", 105)
    del current["protection"]  # missing compares as the PVE default, off
    assert _rows(drift.guest_drift(xur, current, None, [])) == [
        ("protection", "1", "0", drift.LIVE)
    ]
    current["protection"] = "0"
    assert _rows(drift.guest_drift(xur, current, None, [])) == [
        ("protection", "1", "0", drift.LIVE)
    ]

    zion = real_specs()["zion"]
    vm = fixture_live("cluster", 109)
    vm["protection"] = "1"
    assert _rows(drift.unmanaged_diffs(zion, vm)) == [("protection", "-", "1", drift.UNMANAGED)]
    declared = parse_guest("zion", {**default_registry(ROOT).get("zion", "pve-guest"),
                                    "protection": False}, NODES)
    assert _rows(drift.scalar_diffs(declared, vm)) == [("protection", "0", "1", drift.LIVE)]


def test_vm_identity_and_efidisk_and_model() -> None:
    spec = real_specs()["zion"]
    current = fixture_live("cluster", 109)
    current["name"] = "home-assistant"
    current["efidisk0"] = "vm-flash:vm-109-disk-0,efitype=2m,pre-enrolled-keys=1"
    current["net0"] = current["net0"].replace("virtio=", "e1000=")
    current["digest"] = "ignored"
    current["sockets"] = "1"

    rows = {r.key: (r.want, r.have, r.impact) for r in drift.guest_drift(spec, current, None, [])}

    assert rows["name"] == ("zion", "home-assistant", drift.LIVE)
    assert rows["efidisk0.efitype"] == ("4m", "2m", drift.REFUSED)
    assert rows["efidisk0.pre-enrolled-keys"] == ("0", "1", drift.REFUSED)
    assert rows["net0.model"] == ("virtio", "e1000", drift.REBOOT)
    assert "digest" not in rows
    assert "sockets" not in rows


def test_replication_job_count_and_disable() -> None:
    spec = real_specs()["arc"]
    job = pveconf.LiveReplication("104-0", "ace", "*/15", True)
    other = pveconf.LiveReplication("104-1", "clovis", "*/15", False)

    assert [(r.key, r.want, r.have) for r in drift.replication_diffs(spec, [job])] == [
        ("replication.disabled", "no", "yes")
    ]
    assert drift.replication_diffs(spec, [job, other])[0].have == "104-0, 104-1"
    assert drift.replication_diffs(spec, [])[0].have == "-"
    standalone = real_specs()["xur"]
    assert drift.replication_diffs(standalone, [other])[0].have == "104-1 -> clovis"


def test_ha_presence_and_extra_rules() -> None:
    spec = real_specs()["arc"]
    two_rules = pveconf.LiveHa({"bray": 3, "ace": 2}, True, True, ("a", "b"), ())
    no_rule = pveconf.LiveHa({}, False, True, (), ())

    assert drift.ha_diffs(spec, None)[0].have == "-"
    assert drift.ha_diffs(real_specs()["xur"], two_rules)[0].want == "-"
    assert drift.ha_diffs(real_specs()["xur"], None) == []
    assert [r.key for r in drift.ha_diffs(spec, two_rules)] == ["ha.rules"]
    assert [r.have for r in drift.ha_diffs(spec, no_rule)][0] == "no rule"
    failback = pveconf.LiveHa({"bray": 3, "ace": 2}, True, False, ("a",), ())
    assert [(r.key, r.impact) for r in drift.ha_diffs(spec, failback)] == [
        ("ha.failback", drift.CLUSTER)
    ]


def test_bind_mount_drift() -> None:
    raw = copy.deepcopy(default_registry(ROOT).get("xur", "pve-guest"))
    raw["disks"]["mp1"] = {
        "role": "data", "source": "/srv/a", "mount": "/a", "shared": True, "backup": False
    }
    spec = parse_guest("xur", raw, NODES)
    current = fixture_live("osiris", 105)
    current["mp1"] = "/srv/b,mp=/a"

    rows = {r.key: (r.want, r.have, r.impact) for r in drift.disk_diffs(spec, current)}

    assert rows == {
        "mp1.source": ("/srv/a", "/srv/b", drift.REBOOT),
        "mp1.shared": ("yes", "no", drift.CONFIG),
    }


def test_storage_move_is_refused_and_unsized_mount_is_left_alone() -> None:
    spec = real_specs()["tower"]
    current = fixture_live("cluster", 101)
    current["mp3"] = current["mp3"].replace("vault-hdd:", "vm-flash:")
    current["mp1"] = current["mp1"].replace("size=0T", "size=100G")

    rows = {r.key: (r.want, r.have, r.impact) for r in drift.disk_diffs(spec, current)}

    assert rows == {"mp3.storage": ("vault-hdd", "vm-flash", drift.REFUSED)}


def test_format_diff_columns() -> None:
    row = drift.diff("memory", 8192, None, drift.REBOOT)
    assert drift.format_diff(row) == f"{'memory':<22} want={'8192':<26} have={'-':<26} needs reboot"


# --------------------------------------------------------------------------------------
# live report
# --------------------------------------------------------------------------------------


def test_cluster_read_falls_back_past_a_down_home_node() -> None:
    spec = real_specs()["helm"]
    runner = FixtureRunner(down=("clovis",))

    view = live.read_cluster(runner, live.query_nodes(spec, NODES))

    assert view.node == "ace"
    assert live.query_nodes(spec, NODES) == ["clovis", "ace", "bray"]
    assert live.guest_report(spec, view, runner)[0] == "qemu 112 on clovis, running: in sync"
    assert runner.calls[-1] == ("ace", live.pending_command("clovis", "qemu", 112))


def test_standalone_guest_asks_only_its_node() -> None:
    spec = real_specs()["xur"]
    assert live.query_nodes(spec, NODES) == ["osiris"]
    assert live.view_key(spec, NODES) == "osiris"
    assert live.view_key(real_specs()["neo"], NODES) == "cluster"
    with pytest.raises(live.NodeUnreachable, match="no PVE node answered"):
        live.read_cluster(FixtureRunner(down=("osiris",)), ["osiris"])


def test_report_for_missing_moved_and_mistyped_guests() -> None:
    spec = real_specs()["helm"]
    view = live.read_cluster(FixtureRunner(), ["clovis"])
    resources = dict(view.resources)

    missing = live.ClusterView("clovis", {}, view.ha, view.replication)
    assert live.guest_report(spec, missing, FixtureRunner()) == [
        "MISSING: no guest 112 on clovis's cluster; it would be created on clovis "
        "by `./guest create helm`"
    ]

    resources[112] = {**resources[112], "type": "lxc"}
    mistyped = live.ClusterView("clovis", resources, view.ha, view.replication)
    assert live.guest_report(spec, mistyped, FixtureRunner()) == [
        "refused: vmid 112 is lxc, the spec says qemu"
    ]

    resources[112] = {**resources[112], "type": "qemu", "node": "ace"}
    moved = live.ClusterView("clovis", resources, view.ha, view.replication)
    report = live.guest_report(spec, moved, FixtureRunner())
    assert report[0] == "qemu 112 on ace, running (home node clovis): in sync"


def test_report_lists_diffs_and_pending_changes() -> None:
    spec = real_specs()["helm"]
    view = live.read_cluster(FixtureRunner(), ["clovis"])
    entries = {
        entry["key"]: entry
        for entry in json.loads((FIXTURES / "cluster" / "pending-112.json").read_text())
    }
    entries["memory"] = {"key": "memory", "value": "6144", "pending": "8192"}
    entries["cores"] = {"key": "cores", "value": 2, "pending": 4}
    entries["scsi1"] = {**entries["scsi1"], "delete": 1}

    def runner(node: str, command: str) -> str:
        return json.dumps(list(entries.values()))

    report = live.guest_report(spec, view, runner)

    assert report[0] == "qemu 112 on clovis, running: 1 diff(s)"
    assert report[1] == "  " + drift.format_diff(drift.diff("cores", 4, 2, drift.REBOOT))
    assert report[2:] == [
        "  pending cores: 2 -> 4",
        "  pending memory: 6144 -> 8192",
        "  pending scsi1: "
        f"{entries['scsi1']['value']} -> (delete)",
    ]


def test_deploy_host_offline_skips_the_live_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    def no_runner(node: str, command: str) -> str:
        raise AssertionError("offline must not read the cluster")

    pve_guest.deploy_host(ROOT, "helm", True, {}, no_runner)

    assert "qemu 112: live drift skipped (offline validation)" in capsys.readouterr().out


def test_deploy_host_reads_the_cluster_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    runner = FixtureRunner()
    views: dict = {}

    pve_guest.deploy_host(ROOT, "helm", True, views, runner)
    pve_guest.deploy_host(ROOT, "neo", False, views, runner)

    out = capsys.readouterr().out
    assert "qemu 112 on clovis, running: in sync" in out
    assert "lxc 108 on bray, running: in sync" in out
    assert out.count("report only: pve-guest has no apply path") == 1
    assert [c for _, c in runner.calls].count(live.RESOURCES) == 1
    assert set(views) == {"cluster"}


def test_deploy_validates_the_inventory_and_visits_guests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homelab.deploy import DeploySession

    monkeypatch.setenv("HOMELAB_OFFLINE", "1")
    assert pve_guest.deploy(ROOT, "helm", True, False, DeploySession("pve-guest")) == 0
    assert pve_guest.deploy(ROOT, "ace", True, False, DeploySession("pve-guest")) == 0


class FakeResult:
    def __init__(self, exited: int, stdout: str = "") -> None:
        self.exited = exited
        self.stdout = stdout


class FakeConnection:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.commands: list[str] = []

    def run(self, command: str, hide: bool, warn: bool):
        self.commands.append(command)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (OSError("No route to host"), "clovis: No route to host"),
        (FakeResult(255), "clovis: `x` exited 255"),
    ],
)
def test_ssh_runner_turns_failures_into_node_unreachable(
    monkeypatch: pytest.MonkeyPatch, outcome, message: str
) -> None:
    fake = FakeConnection(outcome)
    monkeypatch.setattr(live, "connection_for_host", lambda root, node: type("C", (), {
        "connection": fake})())

    with pytest.raises(live.NodeUnreachable, match=message):
        live.ssh_runner(ROOT)("clovis", "x")


def test_ssh_runner_reuses_one_connection_per_node(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    fake = FakeConnection(FakeResult(0, "[]"))

    def connect(root, node):
        opened.append(node)
        return type("C", (), {"connection": fake})()

    monkeypatch.setattr(live, "connection_for_host", connect)
    run = live.ssh_runner(ROOT)

    assert run("ace", "a") == "[]"
    assert run("ace", "b") == "[]"
    assert opened == ["ace"]
    assert fake.commands == ["a", "b"]


# --------------------------------------------------------------------------------------
# pveconf: normalization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "size"),
    [("32G", 32 << 30), ("0T", None), ("528K", 528 << 10), ("1.5G", 3 << 29), ("512", 512),
     (None, None)],
)
def test_parse_size(text, size) -> None:
    assert pveconf.parse_size(text) == size


def test_parse_size_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="invalid size"):
        pveconf.parse_size("32GB")


@pytest.mark.parametrize(
    ("size", "text"), [(None, "none"), (1 << 40, "1T"), (64 << 30, "64G"), (528 << 10, "528K"),
                       (3 << 20, "3M"), (1000, "1000")]
)
def test_format_size(size, text) -> None:
    assert pveconf.format_size(size) == text


def test_same_value_normalizes_option_lists_only_where_order_is_meaningless() -> None:
    assert pveconf.same_value("agent", "enabled=1", "1")
    assert pveconf.same_value("dev1", "/dev/x,uid=1,gid=2", "/dev/x,gid=2,uid=1")
    assert pveconf.same_value("features", "keyctl=1,nesting=1", "nesting=1,keyctl=1")
    assert not pveconf.same_value("dev1", "/dev/x,uid=1", "/dev/y,uid=1")
    assert not pveconf.same_value("cpu", "host", "x86-64-v3")
    assert not pveconf.same_value("cpu", "host", None)
    assert pveconf.same_value("cores", "4", "4")
    assert pveconf.kv_form("0") == {"enabled": "0"}


def test_parse_disk_variants() -> None:
    vm = pveconf.parse_disk("qemu", "scsi1", "vm-flash:vm-1-disk-2,backup=0,iothread=1,size=64G")
    assert vm == pveconf.LiveDisk("vm-flash", None, 64 << 30, False, True, None, False,
                                  {"iothread": "1"})
    ct_mp = pveconf.parse_disk("lxc", "mp1", "vm-flash:subvol-1-disk-1,mp=/a,size=0T")
    assert (ct_mp.backup, ct_mp.size, ct_mp.mount) == (False, None, "/a")
    rootfs = pveconf.parse_disk("lxc", "rootfs", "vm-flash:subvol-1-disk-0,size=8G")
    assert rootfs.backup is True
    bind = pveconf.parse_disk("lxc", "mp2", "/srv/x,mp=/x,shared=1,replicate=0")
    assert (bind.storage, bind.source, bind.shared, bind.replicate) == (None, "/srv/x", True, False)


def test_parse_net_variants() -> None:
    vm = pveconf.parse_net("qemu", "virtio=bc:24:11:18:e3:17,bridge=vmbr0,firewall=1,tag=40")
    assert (vm.mac, vm.model, vm.tag, vm.firewall, vm.name) == (
        "BC:24:11:18:E3:17", "virtio", 40, True, None)
    ct = pveconf.parse_net("lxc", "name=nic0,hwaddr=BC:24:11:D8:F0:7F,ip=10.0.0.85/24,gw=10.0.0.1")
    assert (ct.mac, ct.model, ct.tag, ct.firewall, ct.ip, ct.gw, ct.bridge) == (
        "BC:24:11:D8:F0:7F", None, None, False, "10.0.0.85/24", "10.0.0.1", "vmbr0")
    odd = pveconf.parse_net("qemu", "bridge=vmbr0")
    assert (odd.mac, odd.model) == (None, None)


def test_parse_pending_splits_current_from_queued() -> None:
    current, pending = pveconf.parse_pending(
        [
            {"key": "cores", "value": 2},
            {"key": "memory", "value": "1024", "pending": "2048"},
            {"key": "mp0", "value": "x", "delete": 1},
            {"key": "net1", "pending": "name=nic1"},
        ]
    )
    assert current == {"cores": "2", "memory": "1024", "mp0": "x"}
    assert pending == [
        pveconf.PendingChange("memory", "1024", "2048", False),
        pveconf.PendingChange("mp0", "x", None, True),
        pveconf.PendingChange("net1", None, "name=nic1", False),
    ]


def test_parse_ha_reads_rules_and_failback() -> None:
    ha = pveconf.parse_ha(
        [{"sid": "vm:1"}, {"sid": "ct:2", "failback": 0}, {"sid": "vm:3"}],
        [
            {"type": "node-affinity", "rule": "r1", "nodes": "a:3,b:2", "strict": 1,
             "resources": "vm:1,ct:2"},
            {"type": "node-affinity", "rule": "r2", "nodes": "a", "resources": "ct:2"},
            {"type": "resource-affinity", "rule": "r3", "resources": "vm:3,vm:1"},
        ],
    )
    assert ha["vm:1"] == pveconf.LiveHa({"a": 3, "b": 2}, True, True, ("r1",), ("ct:2",))
    assert ha["ct:2"].rules == ("r1", "r2")
    assert ha["ct:2"].failback is False
    assert ha["vm:3"] == pveconf.LiveHa({}, False, True, (), ())
    assert pveconf.parse_ha_nodes("a") == {"a": 0}


def test_parse_replication_groups_jobs_by_guest() -> None:
    jobs = pveconf.parse_replication(
        [
            {"guest": 108, "id": "108-1", "target": "ace", "schedule": "*/15"},
            {"guest": 108, "id": "108-2", "target": "clovis", "disable": 1},
        ]
    )
    assert jobs == {
        108: [
            pveconf.LiveReplication("108-1", "ace", "*/15", False),
            pveconf.LiveReplication("108-2", "clovis", "*/15", True),
        ]
    }


def test_key_classifiers() -> None:
    assert pveconf.is_disk_key("qemu", "scsi1")
    assert not pveconf.is_disk_key("qemu", "mp0")
    assert pveconf.is_disk_key("lxc", "mp10")
    assert not pveconf.is_disk_key("lxc", "scsi0")
    assert pveconf.is_net_key("net3")
    assert not pveconf.is_net_key("netx")
    assert pveconf.ha_sid("qemu", 5) == "vm:5"
    assert pveconf.ha_sid("lxc", 5) == "ct:5"


# --------------------------------------------------------------------------------------
# spec validation
# --------------------------------------------------------------------------------------


def base_vm() -> dict:
    return {
        "vmid": 200,
        "kind": "qemu",
        "home_node": "bray",
        "profile": "linux-vm",
        "cores": 2,
        "memory": 2048,
        "disks": {"scsi0": {"role": "os", "size": "16G"}},
        "net": {"net0": {"name": "nic0", "mac": "BC:24:11:00:00:00", "tag": 40}},
        "ha": {"nodes": {"bray": 3, "ace": 2}},
        "replication": {"target": "ace"},
    }


def base_ct() -> dict:
    return {
        "vmid": 201,
        "kind": "lxc",
        "home_node": "osiris",
        "profile": "plain-ct",
        "cores": 1,
        "memory": 512,
        "disks": {"rootfs": {"role": "os", "size": "8G"}},
        "net": {"net0": {"name": "nic0", "mac": "BC:24:11:00:00:01"}},
    }


def test_valid_specs_parse_with_defaults() -> None:
    vm = parse_guest("g", base_vm(), NODES)
    assert vm.scalars["boot"] == "order=scsi0"
    assert vm.scalars["name"] == "g"
    assert vm.ha.strict is True and vm.ha.failback is True
    assert vm.replication.schedule == "*/15"
    assert vm.disks[0].storage == "vm-flash"
    assert vm.nets[0].firewall is True and vm.nets[0].ip is None
    ct = parse_guest("c", base_ct(), NODES)
    assert ct.scalars["hostname"] == "c"
    assert ct.scalars["features"] == "nesting=1"
    assert ct.nets[0].ip == "dhcp"
    assert ct.name_key == "hostname" and vm.name_key == "name"


def test_profile_keys_are_overridable() -> None:
    raw = base_vm() | {"cpu": "host", "onboot": False}
    spec = parse_guest("g", raw, NODES)
    assert spec.scalars["cpu"] == "host"
    assert spec.scalars["onboot"] == "0"
    assert to_pve(True) == "1"


def _set(raw: dict, path: str, value) -> dict:
    *parents, last = path.split(".")
    node = raw
    for part in parents:
        node = node[part]
    if value is _DELETE:
        del node[last]
    else:
        node[last] = value
    return raw


_DELETE = object()


V = "g: pve-guest"


@pytest.mark.parametrize(
    ("base", "path", "value", "message"),
    [
        (base_vm, "kind", "vm", f"{V}.kind must be one of lxc, qemu, got 'vm'"),
        (base_vm, "kind", ["qemu"], f"{V}.kind must be one of"),
        (base_vm, "profile", "nope", f"{V}.profile must be one of docker-ct, linux-vm, plain-ct"),
        (base_vm, "profile", "docker-ct", f"{V}.profile docker-ct is for lxc guests"),
        (base_vm, "home_node", "helm", f"{V}.home_node must be one of"),
        (base_vm, "colour", "red", f"{V}: unknown key(s) colour"),
        (base_vm, "vmid", 0, f"{V}.vmid must be a positive integer"),
        (base_vm, "vmid", True, f"{V}.vmid must be a positive integer"),
        (base_vm, "cores", "2", f"{V}.cores must be a positive integer"),
        (base_vm, "memory", _DELETE, f"{V}.memory must be a positive integer"),
        (base_vm, "balloon", 4096, f"{V}.balloon must be an integer from 0 to memory"),
        (base_vm, "balloon", -1, f"{V}.balloon must be an integer from 0 to memory"),
        (base_vm, "balloon", True, f"{V}.balloon must be an integer from 0 to memory"),
        (base_vm, "balloon", 0, None),
        (base_ct, "balloon", 256, f"{V}.balloon is a qemu-only key"),
        (base_vm, "cpu", {"a": 1}, f"{V}.cpu must be a scalar"),
        (base_vm, "cpu", None, f"{V}.cpu must be a scalar"),
        (base_vm, "extra", {"cores": 4}, f"{V}.extra.cores is not an allowlisted rare key"),
        (base_vm, "extra", {"cpu": "host"}, f"{V}.extra.cpu is not an allowlisted"),
        (base_vm, "extra", {"startup": ["x"]}, f"{V}.extra.startup must be a scalar"),
        (base_vm, "extra", ["startup"], f"{V}.extra must be a mapping"),
        (base_vm, "disks", [], f"{V}.disks must be a mapping"),
        (base_vm, "disks.scsi0.role", "scratch", f"{V}.disks needs exactly one role: os disk"),
        (base_vm, "disks.scsi1", {"role": "os", "size": "1G"}, f"{V}.disks needs exactly one"),
        (base_vm, "disks.rootfs", {"role": "data"}, f"{V}.disks.rootfs: not a qemu disk key"),
        (base_ct, "disks.scsi0", {"role": "data"}, f"{V}.disks.scsi0: not a lxc disk key"),
        (base_ct, "disks.rootfs.role", "appdata", f"{V}.disks needs exactly one role: os"),
        (base_vm, "disks.scsi0", "os", f"{V}.disks.scsi0 must be a mapping"),
        (base_vm, "disks.scsi0.b", 1, f"{V}.disks.scsi0: unknown field(s) b"),
        (base_vm, "disks.scsi0.role", "cache", f"{V}.disks.scsi0.role must be one of"),
        (base_vm, "disks.scsi0.size", _DELETE, f"{V}.disks.scsi0.size is required"),
        (base_ct, "disks.rootfs.size", _DELETE, f"{V}.disks.rootfs.size is required"),
        (base_vm, "disks.scsi0.size", "10GB", f"{V}.disks.scsi0.size: invalid size '10GB'"),
        (base_vm, "disks.scsi0.mount", "/x", f"{V}.disks.scsi0: an os disk has no mount"),
        (base_vm, "disks.scsi0.backup", "no", f"{V}.disks.scsi0.backup must be true or false"),
        (base_vm, "disks.scsi0.replicate", 1, f"{V}.disks.scsi0.replicate must be true or"),
        (base_vm, "disks.scsi1", {"role": "data", "size": "1G"}, f"{V}.disks.scsi1.mount must"),
        (base_vm, "disks.scsi1", {"role": "data", "size": "1G", "mount": "x"}, f"{V}.disks"),
        (base_vm, "disks.scsi1", {"role": "appdata", "size": "1G", "mount": "/mnt/s"}, None),
        (base_vm, "disks.scsi1", {"role": "data", "size": "1G", "source": "/x"},
         f"{V}.disks.scsi1.source must be an absolute host path on an lxc mpN"),
        (base_ct, "disks.mp0", {"role": "data", "source": "x", "mount": "/x"},
         f"{V}.disks.mp0.source must be"),
        (base_ct, "disks.mp0", {"role": "data", "source": "/x", "size": "1G"},
         f"{V}.disks.mp0: a host bind mount takes no size or storage"),
        (base_ct, "disks.mp0", {"role": "data", "source": "/x", "storage": "s", "mount": "/x"},
         f"{V}.disks.mp0: a host bind mount takes no size or storage"),
        (base_ct, "disks.mp0", {"role": "data", "mount": "/x", "shared": True},
         f"{V}.disks.mp0.shared only applies to a host bind mount"),
        (base_ct, "disks.mp0", {"role": "data", "source": "/x", "mount": "/x", "shared": 1},
         f"{V}.disks.mp0.shared must be true or false"),
        (base_vm, "net", {}, f"{V}.net needs at least one NIC"),
        (base_vm, "net", None, f"{V}.net must be a mapping"),
        (base_vm, "net.eth0", {"name": "nic0"}, f"{V}.net.eth0: not a netN key"),
        (base_vm, "net.net0", "nic0", f"{V}.net.net0 must be a mapping"),
        (base_vm, "net.net0.name", "eth0", f"{V}.net.net0.name must be nic0"),
        (base_vm, "net.net0.mac", "bc:24:11:00:00:00", f"{V}.net.net0.mac must be an upper-case"),
        (base_vm, "net.net0.mac", _DELETE, f"{V}.net.net0.mac must be an upper-case"),
        (base_vm, "net.net0.tag", 0, f"{V}.net.net0.tag must be a VLAN id from 1 to 4094"),
        (base_vm, "net.net0.tag", 4095, f"{V}.net.net0.tag must be a VLAN id"),
        (base_vm, "net.net0.tag", "40", f"{V}.net.net0.tag must be a VLAN id"),
        (base_vm, "net.net0.tag", True, f"{V}.net.net0.tag must be a VLAN id"),
        (base_vm, "net.net0.tag", 1, None),
        (base_vm, "net.net0.tag", 4094, None),
        (base_vm, "net.net0.a", 1, f"{V}.net.net0: unknown field(s) a"),
        (base_vm, "net.net0.ip", "dhcp", f"{V}.net.net0: ip/gw are lxc-only"),
        (base_vm, "net.net0.gw", "10.0.0.1", f"{V}.net.net0: ip/gw are lxc-only"),
        (base_ct, "net.net0.gw", "10.0.0.1", f"{V}.net.net0.gw needs a static ip"),
        (base_ct, "net.net0.ip", "10.0.0.300/24", f"{V}.net.net0.ip: "),
        (base_ct, "net.net0.ip", "10.0.0.5", f"{V}.net.net0.ip needs a prefix length"),
        (base_vm, "net.net0.firewall", 1, f"{V}.net.net0.firewall must be true or false"),
        (base_vm, "ha", [], f"{V}.ha must be a mapping"),
        (base_vm, "ha.nodes", None, f"{V}.ha.nodes must be a mapping"),
        (base_vm, "ha.nodes", {}, f"{V}.ha.nodes must name at least one node"),
        (base_vm, "ha.nodes", {"bray": -1}, f"{V}.ha.nodes.bray must be a priority >= 0"),
        (base_vm, "ha.nodes", {"bray": True}, f"{V}.ha.nodes.bray must be a priority"),
        (base_vm, "ha.nodes", {"bray": "3", "ace": 1}, f"{V}.ha.nodes.bray must be a priority"),
        (base_vm, "ha.nodes", {"bray": 0, "ace": 0}, None),
        (base_vm, "ha.nodes", {"bray": 1, "ace": 1, "clovis": 1}, None),
        (base_vm, "ha.strict", "yes", f"{V}.ha.strict must be true or false"),
        (base_vm, "ha.failback", 0, f"{V}.ha.failback must be true or false"),
        (base_vm, "ha.g", "x", f"{V}.ha: unknown field(s) g"),
        (base_vm, "ha.nodes", {"ace": 3}, f"{V}.ha.nodes must be cluster nodes including bray"),
        (base_vm, "ha.nodes", {"bray": 3, "osiris": 1}, f"{V}.ha.nodes must be cluster nodes"),
        (base_vm, "replication", _DELETE, f"{V}: an HA guest needs replication"),
        (base_vm, "replication", "ace", f"{V}.replication must be a mapping"),
        (base_vm, "replication", {"target": "ace", "rate": 10}, f"{V}.replication takes a target"),
        (base_vm, "replication", {"schedule": "*/5"}, f"{V}.replication takes a target"),
        (base_vm, "replication.target", "bray", f"{V}.replication.target must be another"),
        (base_vm, "replication.target", "osiris", f"{V}.replication.target must be another"),
        (base_vm, "replication.target", "clovis", f"{V}.replication.target must be one of ha"),
        (base_ct, "replication", {"target": "ace"}, f"{V}: standalone node osiris has no HA"),
        (base_ct, "ha", {"nodes": {"osiris": 1}}, f"{V}: standalone node osiris has no HA"),
        (base_vm, "disks.scsi0.replicate", False,
         f"{V}.disks.scsi0: an HA guest must replicate every disk"),
    ],
)
def test_spec_rejects(base, path: str, value, message: str | None) -> None:
    raw = _set(base(), path, value)
    if message is None:
        parse_guest("g", raw, NODES)
        return
    with pytest.raises(ValueError, match="^" + re.escape(message)):
        parse_guest("g", raw, NODES)


P = f"{V}.protection must be true: an in-place restore would destroy backup: false data on"
UNBACKED = {"role": "data", "mount": "/d", "backup": False}


@pytest.mark.parametrize(
    ("base", "disks", "protection", "message"),
    [
        (base_ct, {"mp0": UNBACKED}, _DELETE, f"{P} mp0"),
        (base_ct, {"mp0": UNBACKED}, False, f"{P} mp0"),
        (base_ct, {"mp0": UNBACKED, "mp2": {**UNBACKED, "mount": "/e"}}, _DELETE, f"{P} mp0, mp2"),
        (base_ct, {"mp0": UNBACKED}, True, None),
        (base_ct, {"mp0": UNBACKED}, "yes", f"{V}.protection must be true or false"),
        # A host path is not the CT's volume, so a restore never destroys it.
        (base_ct, {"mp0": {**UNBACKED, "source": "/srv/d"}}, _DELETE, None),
        (base_ct, {"mp0": {"role": "data", "mount": "/d"}}, _DELETE, None),
        (base_ct, {"mp0": {"role": "scratch"}}, _DELETE, None),
        # An in-place qmrestore keeps excluded disks as unusedN (homelab-ops#160).
        (base_vm, {"scsi1": {**UNBACKED, "size": "1G"}}, _DELETE, None),
    ],
)
def test_protection_is_required_on_cts_with_unbacked_data(
    base, disks: dict, protection, message: str | None
) -> None:
    raw = base()
    raw["disks"] |= disks
    if protection is not _DELETE:
        raw["protection"] = protection
    if message is None:
        parse_guest("g", raw, NODES)
        return
    with pytest.raises(ValueError, match="^" + re.escape(message) + "$"):
        parse_guest("g", raw, NODES)


def test_protection_is_managed_only_where_declared() -> None:
    assert "protection" not in parse_guest("g", base_ct(), NODES).scalars
    assert parse_guest("g", base_ct() | {"protection": True}, NODES).scalars["protection"] == "1"
    assert parse_guest("g", base_vm() | {"protection": False}, NODES).scalars["protection"] == "0"


def test_unknown_keys_are_all_listed() -> None:
    raw = base_vm() | {"b": 1, "a": 2}
    with pytest.raises(ValueError, match=re.escape(f"{V}: unknown key(s) a, b")):
        parse_guest("g", raw, NODES)
    raw = base_vm()
    raw["disks"]["scsi0"] |= {"y": 1, "x": 1}
    with pytest.raises(ValueError, match=re.escape("scsi0: unknown field(s) x, y")):
        parse_guest("g", raw, NODES)
    raw = base_vm()
    raw["net"]["net0"] |= {"y": 1, "x": 1}
    with pytest.raises(ValueError, match=re.escape("net0: unknown field(s) x, y")):
        parse_guest("g", raw, NODES)
    raw = base_vm()
    raw["ha"] |= {"y": 1, "x": 1}
    with pytest.raises(ValueError, match=re.escape(".ha: unknown field(s) x, y")):
        parse_guest("g", raw, NODES)


def test_a_bad_gateway_names_the_gw_field() -> None:
    raw = base_ct()
    raw["net"]["net0"] |= {"ip": "10.0.0.5/24", "gw": "10.0.0.300"}
    with pytest.raises(ValueError, match=re.escape(f"{V}.net.net0.gw: ")):
        parse_guest("g", raw, NODES)


def test_parsed_fields_carry_their_values() -> None:
    raw = base_ct() | {"home_node": "bray", "replication": {"target": "ace", "schedule": "*/5"},
                       "protection": True}
    raw["ha"] = {"nodes": {"bray": 2, "ace": 1}, "strict": False, "failback": False}
    raw["net"]["net0"] |= {"bridge": "vmbr1", "tag": 10, "firewall": False,
                           "ip": "10.0.0.5/24", "gw": "10.0.0.1"}
    raw["disks"]["mp0"] = {"role": "data", "mount": "/d", "storage": "vault-hdd",
                           "backup": False, "size": "1T"}
    spec = parse_guest("g", raw, NODES)

    assert spec.profile == "plain-ct"
    assert spec.ha == HaSpec({"bray": 2, "ace": 1}, False, False)
    assert spec.replication == ReplicationSpec("ace", "*/5")
    assert spec.nets == (
        NetSpec("net0", "nic0", "BC:24:11:00:00:01", "vmbr1", 10, False, "10.0.0.5/24",
                "10.0.0.1"),
    )
    assert spec.disks[1] == DiskSpec("mp0", "data", 1 << 40, "vault-hdd", None, False, True,
                                     "/d", False)


def test_ha_ct_bind_mount_must_be_shared() -> None:
    raw = base_ct() | {"home_node": "bray", "ha": {"nodes": {"bray": 1}},
                       "replication": {"target": "ace"}}
    raw["ha"]["nodes"]["ace"] = 1
    raw["disks"]["mp0"] = {"role": "data", "source": "/srv/x", "mount": "/x"}
    with pytest.raises(ValueError, match="must set shared: true"):
        parse_guest("c", raw, NODES)
    raw["disks"]["mp0"]["shared"] = True
    assert parse_guest("c", raw, NODES).disks[1].shared is True


def test_lxc_os_disk_must_be_rootfs() -> None:
    raw = base_ct()
    raw["disks"] = {"rootfs": {"role": "appdata", "size": "8G"}, "mp0": {"role": "os"}}
    with pytest.raises(ValueError, match="the lxc os disk is rootfs"):
        parse_guest("c", raw, NODES)


def test_duplicate_mounts_are_rejected() -> None:
    raw = base_ct()
    raw["disks"]["mp0"] = {"role": "appdata"}
    raw["disks"]["mp1"] = {"role": "data", "mount": "/mnt/cache/appdata"}
    raw["disks"]["mp2"] = {"role": "scratch"}
    raw["disks"]["mp3"] = {"role": "data", "mount": "/mnt/scratch"}
    message = "c: pve-guest.disks: mount path used twice: /mnt/cache/appdata, /mnt/scratch"
    with pytest.raises(ValueError, match="^" + re.escape(message)):
        parse_guest("c", raw, NODES)


def test_non_mapping_blocks_are_rejected() -> None:
    with pytest.raises(ValueError, match="g: pve-guest must be a mapping"):
        parse_guest("g", None, NODES)
    raw = base_vm()
    raw["net"]["net0"] = "nic0"
    with pytest.raises(ValueError, match="net.net0 must be a mapping"):
        parse_guest("g", raw, NODES)


# --------------------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------------------


def _registry(tmp_path: Path, extra: dict) -> HostRegistry:
    def node(standalone: bool = False) -> dict:
        config = {"type": "pve", "hostname": "n", "user": "root", "sshkey": "infra"}
        if standalone:
            config["standalone"] = True
        return {"config": config, "features": {}}

    data = {"ace": node(), "bray": node(), "clovis": node(), "osiris": node(True)}
    data.update(extra)
    path = tmp_path / "hosts.conf"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return HostRegistry(path)


def _guest(block: dict) -> dict:
    return {
        "config": {"type": "ubuntu", "hostname": "g", "user": "root", "sshkey": "infra"},
        "features": {"pve-guest": block},
    }


def test_inventory_reads_nodes_and_gated_guests(tmp_path: Path) -> None:
    gated = base_ct() | {"deploy": False}
    registry = _registry(tmp_path, {"vm": _guest(base_vm()), "ct": _guest(gated)})

    assert pve_nodes(registry) == NODES
    assert declared_guests(registry) == ["vm", "ct"]
    assert registry.list_hosts(feature="pve-guest") == ["vm"]
    assert validate_inventory(registry) == (2, 0)


def test_inventory_rejects_duplicate_vmids_and_node_blocks(tmp_path: Path) -> None:
    twin = base_vm()
    registry = _registry(tmp_path, {"a": _guest(base_vm()), "b": _guest(twin)})
    with pytest.raises(ValueError, match="b: vmid 200 is already used by a"):
        load_guests(registry)

    registry = _registry(tmp_path, {})
    data = registry.load()
    data["ace"]["features"]["pve-guest"] = base_vm()
    with pytest.raises(ValueError, match="ace: pve-guest is for guests, not PVE nodes"):
        load_guests(registry)


def _with_refs(tmp_path: Path, osiris_features: dict, ace_features: dict) -> HostRegistry:
    registry = _registry(tmp_path, {"ct": _guest(base_ct()), "vm": _guest(base_vm())})
    registry.load()["osiris"]["features"].update(osiris_features)
    registry.load()["ace"]["features"].update(ace_features)
    return registry


def _plans(*plans: dict) -> dict:
    return {"zfs-automation": {"migratable_lxc_groups": {"cluster": {"plans": list(plans)}}}}


@pytest.mark.parametrize(
    ("osiris_features", "ace_features", "message"),
    [
        ({"pve-backup": {"restore_lxc_configs": {"vmids": [201]}}}, {}, None),
        ({"pve-backup": {"restore_lxc_configs": {"vmids": [999]}}}, {}, "vmid 999 is not"),
        ({"pve-backup": {"restore_lxc_configs": {"vmids": [200]}}}, {}, "is qemu, expected lxc"),
        ({"pve-backup": {"restore_lxc_configs": {"vmids": ["x"]}}}, {}, "'x' is not a vmid"),
        ({"pve-backup": {"restore_lxc_configs": {"vmids": [-1]}}}, {}, "-1 is not a vmid"),
        ({"pve-backup": {"pbs_setup": {"jobs": [{"exclude": "201, 200"}]}}}, {},
         "vmid 200 \\(vm\\) lives on bray, not osiris"),
        ({"pve-backup": {"pbs_setup": {"jobs": [{"vmid": "201"}, {"storage": "x"}]}}}, {}, None),
        ({}, _plans({"vmid": 201, "dataset": "vm-flash/subvol-201-disk-1"}), None),
        ({}, _plans({"vmid": 201, "dataset": "vm-flash/subvol-202-disk-1"}),
         "is not vmid 201's"),
        ({}, _plans({"vmid": 200, "dataset": "pool/x"}), "is qemu, expected lxc"),
        ({}, _plans({"vmid": 200, "kind": "qemu", "dataset": "vm-flash/vm-200-disk-1"}), None),
        ({}, _plans({"vmid": 200, "kind": "qemu", "dataset": "vm-flash/vm-201-disk-1"}),
         "is not vmid 200's"),
        ({}, _plans({"vmid": 201, "kind": "qemu", "dataset": "pool/x"}),
         "is lxc, expected qemu"),
    ],
)
def test_vmid_references(tmp_path: Path, osiris_features, ace_features, message) -> None:
    registry = _with_refs(tmp_path, osiris_features, ace_features)
    guests = load_guests(registry)
    if message is None:
        assert check_vmid_references(registry, guests) >= 1
        return
    with pytest.raises(ValueError, match=message):
        check_vmid_references(registry, guests)


def test_cli_check_pve_guests(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import click

    from homelab import cli

    cli.check_pve_guests(ROOT)
    assert "8 pve-guest spec(s) valid; 21 hand-written vmid(s) match" in capsys.readouterr().out

    def broken(registry):
        raise ValueError("helm: pve-guest.vmid must be a positive integer")

    monkeypatch.setattr(
        "homelab.modules.pve_guest.inventory.validate_inventory", broken
    )
    with pytest.raises(click.ClickException, match="vmid must be a positive integer"):
        cli.check_pve_guests(ROOT)


# --------------------------------------------------------------------------------------
# exact rows and edge parsing (mutation-tested: see [tool.mutmut].only_mutate)
# --------------------------------------------------------------------------------------


def test_to_bool_reads_pve_booleans_only() -> None:
    assert pveconf.to_bool(1) is True
    assert pveconf.to_bool("1") is True
    assert pveconf.to_bool(0) is False
    assert pveconf.to_bool(None) is False


def test_split_prop_and_kv_form_edges() -> None:
    assert pveconf.split_prop("a,b=1") == {"b": "1"}
    assert pveconf.split_prop("vol,stray,k=v=w", "volume") == {"volume": "vol", "k": "v=w"}
    assert pveconf.kv_form("/dev/x,uid=1,a=b=c") == {"_": "/dev/x", "uid": "1", "a": "b=c"}


def test_parse_disk_and_net_edges() -> None:
    nameless = pveconf.parse_disk("lxc", "mp0", "mp=/x,size=1G")
    assert (nameless.storage, nameless.source, nameless.replicate) == (None, None, True)
    assert pveconf.parse_disk("qemu", "scsi0", "a:b:c,size=1G").storage == "a"
    net = pveconf.parse_net("qemu", "virtio=BC:24:11:00:00:00,bridge=vmbr1")
    assert (net.bridge, net.firewall) == ("vmbr1", False)


def test_parse_ha_defaults_when_a_rule_or_flag_is_absent() -> None:
    ha = pveconf.parse_ha(
        [{"sid": "vm:1"}, {"sid": "vm:2", "failback": 0}],
        [{"type": "node-affinity", "rule": "r", "nodes": "a:1", "resources": "vm:1"}],
    )
    assert ha["vm:1"].strict is False
    assert ha["vm:1"].rule_members == ()
    assert ha["vm:2"] == pveconf.LiveHa({}, False, False, (), ())


def test_scalar_impact_table() -> None:
    assert drift.scalar_impact("qemu", "balloon") == drift.LIVE
    assert drift.scalar_impact("qemu", "cores") == drift.REBOOT
    assert drift.scalar_impact("lxc", "arch") == drift.REFUSED
    assert drift.scalar_impact("lxc", "hostname") == drift.REBOOT
    assert drift.scalar_impact("lxc", "dev0") == drift.REBOOT
    assert drift.scalar_impact("lxc", "memory") == drift.LIVE


def _rows(rows) -> list[tuple[str, str, str, str]]:
    return [(r.key, r.want, r.have, r.impact) for r in rows]


def test_vm_without_an_efidisk() -> None:
    spec = real_specs()["riven"]
    current = fixture_live("cluster", 110)
    del current["efidisk0"]
    assert _rows(drift.efidisk_diffs(spec, current)) == [
        ("efidisk0.efitype", "4m", "-", drift.REFUSED),
        ("efidisk0.pre-enrolled-keys", "0", "-", drift.REFUSED),
    ]


def test_every_missing_disk_and_nic_is_reported() -> None:
    spec = real_specs()["neo"]
    current = fixture_live("cluster", 108)
    for key in ("mp0", "mp1", "net0", "net1"):
        del current[key]
    assert [r.key for r in drift.disk_diffs(spec, current)] == ["mp0", "mp1"]
    assert [r.key for r in drift.net_diffs(spec, current)] == ["net0", "net1"]


def test_every_lxc_disk_and_nic_field_has_its_own_row() -> None:
    spec = real_specs()["neo"]
    current = fixture_live("cluster", 108)
    current["mp3"] = "pool:subvol-108-disk-4,mp=/other,replicate=0,shared=1,size=0T"
    current["net1"] = "name=eth1,bridge=vmbr1,hwaddr=BC:24:11:00:00:09,ip=10.0.0.9/24,gw=10.0.0.1"
    rows = _rows(drift.disk_diffs(spec, current) + drift.net_diffs(spec, current))
    assert rows == [
        ("mp3.storage", "vm-flash", "pool", drift.REFUSED),
        ("mp3.backup", "yes", "no", drift.CONFIG),
        ("mp3.replicate", "yes", "no", drift.CONFIG),
        ("mp3.shared", "no", "yes", drift.CONFIG),
        ("mp3.mount", "/mnt/cache/obsidian", "/other", drift.REBOOT),
        ("net1.mac", "BC:24:11:CC:A6:41", "BC:24:11:00:00:09", drift.REFUSED),
        ("net1.bridge", "vmbr0", "vmbr1", drift.LIVE),
        ("net1.tag", "10", "-", drift.LIVE),
        ("net1.firewall", "yes", "no", drift.LIVE),
        ("net1.name", "nic1", "eth1", drift.REBOOT),
        ("net1.ip", "dhcp", "10.0.0.9/24", drift.LIVE),
        ("net1.gw", "-", "10.0.0.1", drift.LIVE),
    ]


def test_vm_disk_options_and_sizes() -> None:
    spec = real_specs()["helm"]
    current = fixture_live("cluster", 112)
    current["scsi0"] = "vm-flash:vm-112-disk-1,iothread=1,size=32G,ssd=0"
    current["scsi1"] = "vm-flash:vm-112-disk-2,backup=0,discard=on,iothread=1,ssd=1"
    assert _rows(drift.disk_diffs(spec, current)) == [
        ("scsi0.discard", "on", "-", drift.REBOOT),
        ("scsi0.ssd", "1", "0", drift.REBOOT),
        ("scsi1.size", "64G", "none", drift.LIVE),
    ]


def test_a_restored_vm_points_at_its_unused_disk_not_a_new_one() -> None:
    """The shape an in-place qmrestore leaves (#160): scsi1 gone, its data in unused0."""
    raw = copy.deepcopy(default_registry(ROOT).get("helm", "pve-guest"))
    raw["disks"]["scsi3"] = {"role": "data", "size": "8G", "storage": "other", "mount": "/d"}
    spec = parse_guest("helm", raw, NODES)
    current = fixture_live("cluster", 112)
    del current["scsi1"]
    current["unused0"] = "vm-flash:vm-112-disk-2"

    rows = drift.disk_diffs(spec, current)

    assert _rows(rows) == [
        ("scsi1", "reattach unused0", "-", drift.CONFIG),
        ("scsi3", "new data disk", "-", drift.LIVE),
    ]
    assert [row.fix for row in rows] == [
        "qm set 112 --scsi1 vm-flash:vm-112-disk-2,backup=0,discard=on,iothread=1,ssd=1",
        None,
    ]
    unmanaged = drift.unmanaged_diffs(spec, current)
    assert _rows(unmanaged) == [("unused0", "-", "vm-flash:vm-112-disk-2", drift.UNMANAGED)]


def test_an_unused_disk_on_another_storage_is_not_offered() -> None:
    spec = real_specs()["helm"]
    current = fixture_live("cluster", 112)
    del current["scsi1"]
    current["unused0"] = "other:vm-112-disk-2"
    (row,) = drift.disk_diffs(spec, current)
    assert (row.key, row.want, row.impact, row.fix) == (
        "scsi1",
        "new scratch disk",
        drift.LIVE,
        None,
    )


def test_an_ambiguous_reattach_names_every_candidate_and_no_command() -> None:
    spec = real_specs()["helm"]
    current = fixture_live("cluster", 112)
    del current["scsi1"]
    current["unused10"] = "vm-flash:vm-112-disk-9"
    current["unused2"] = "vm-flash:vm-112-disk-2"
    (row,) = drift.disk_diffs(spec, current)
    assert (row.want, row.impact, row.fix) == (
        "reattach one of unused2, unused10",
        drift.CONFIG,
        None,
    )


def test_a_ct_mount_point_reattaches_with_pct_unless_two_disks_compete() -> None:
    spec = real_specs()["neo"]
    current = fixture_live("cluster", 108)
    del current["mp1"]
    current["unused0"] = "vm-flash:subvol-108-disk-2"
    (row,) = drift.disk_diffs(spec, current)
    assert (row.key, row.want, row.impact) == ("mp1", "reattach unused0", drift.CONFIG)
    assert row.fix == "pct set 108 --mp1 vm-flash:subvol-108-disk-2,mp=/mnt/cache/pictures"

    del current["mp2"]
    rows = drift.disk_diffs(spec, current)
    assert [(r.key, r.want, r.fix) for r in rows] == [
        ("mp1", "reattach one of unused0", None),
        ("mp2", "reattach one of unused0", None),
    ]


def test_a_missing_bind_mount_is_never_offered_an_unused_volume() -> None:
    raw = copy.deepcopy(default_registry(ROOT).get("neo", "pve-guest"))
    raw["disks"]["mp6"] = {"role": "data", "source": "/srv/x", "mount": "/mnt/x", "shared": True}
    spec = parse_guest("neo", raw, NODES)
    current = fixture_live("cluster", 108)
    current["unused0"] = "vm-flash:subvol-108-disk-9"
    (row,) = drift.disk_diffs(spec, current)
    assert (row.key, row.want, row.fix) == ("mp6", "new data disk", None)


def test_report_prints_the_reattach_command_under_its_row() -> None:
    spec = real_specs()["helm"]
    view = live.read_cluster(FixtureRunner(), ["clovis"])
    entries = [
        entry
        for entry in json.loads((FIXTURES / "cluster" / "pending-112.json").read_text())
        if entry["key"] != "scsi1"
    ]
    entries.append({"key": "unused0", "value": "vm-flash:vm-112-disk-2"})

    report = live.guest_report(spec, view, lambda _node, _command: json.dumps(entries))

    assert report[1:] == [
        "  " + drift.format_diff(drift.diff("scsi1", "reattach unused0", None, drift.CONFIG)),
        "    $ qm set 112 --scsi1 vm-flash:vm-112-disk-2,backup=0,discard=on,iothread=1,ssd=1",
        "  "
        + drift.format_diff(drift.diff("unused0", None, "vm-flash:vm-112-disk-2", drift.UNMANAGED)),
    ]


def test_ha_rows_are_exact() -> None:
    arc = real_specs()["arc"]
    xur = real_specs()["xur"]
    shared = pveconf.LiveHa({"bray": 3, "ace": 2}, True, False, ("r1", "r2"), ("ct:9",))
    assert _rows(drift.ha_diffs(arc, None)) == [
        ("ha", "nodes ace:2,bray:3", "-", drift.CLUSTER)
    ]
    assert _rows(drift.ha_diffs(xur, shared)) == [
        ("ha", "-", "nodes ace:2,bray:3", drift.CLUSTER)
    ]
    assert _rows(drift.ha_diffs(arc, shared)) == [
        ("ha.failback", "yes", "no", drift.CLUSTER),
        ("ha.rules", "one node-affinity rule", "r1, r2", drift.CLUSTER),
    ]


def test_replication_rows_are_exact() -> None:
    arc = real_specs()["arc"]
    moved = pveconf.LiveReplication("104-0", "clovis", "*/5", False)
    other = pveconf.LiveReplication("104-1", "ace", "*/15", False)
    assert _rows(drift.replication_diffs(arc, [moved])) == [
        ("replication.target", "ace", "clovis", drift.CLUSTER),
        ("replication.schedule", "*/15", "*/5", drift.CLUSTER),
    ]
    assert _rows(drift.replication_diffs(arc, [moved, other])) == [
        ("replication", "one job -> ace", "104-0, 104-1", drift.CLUSTER)
    ]
    assert _rows(drift.replication_diffs(arc, [])) == [
        ("replication", "one job -> ace", "-", drift.CLUSTER)
    ]
    assert _rows(drift.replication_diffs(real_specs()["xur"], [moved, other])) == [
        ("replication", "-", "104-0 -> clovis", drift.CLUSTER),
        ("replication", "-", "104-1 -> ace", drift.CLUSTER),
    ]
