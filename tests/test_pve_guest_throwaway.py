"""pve-guest throwaway: a guest built from flags, never named in hosts.conf.

The build itself is `./guest create`'s, covered in test_pve_guest_create.py; this
file covers what is new: the made-up spec, the vmid range, the address report,
and the guards on list and destroy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from homelab import cli
from homelab.hosts import HostRegistry, default_registry
from homelab.modules.pve_guest import build, create, live, throwaway
from homelab.modules.pve_guest.inventory import load_guests
from homelab.modules.pve_guest.spec import MAC_RE, THROWAWAY_VMIDS, parse_guest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pve_guest"
NODES = {"ace": False, "bray": False, "clovis": False, "osiris": True}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest infra"
SUMS = f"{'ab' * 64}  debian-13-generic-amd64-20261111-2659.qcow2\n"
INDEX = '<a href="20261111-2659/">20261111-2659/</a>\n'
INDEX_READ = f"curl -fsSL {build.IMAGE_ROOT}/"
MAC_BYTES = bytes([0xAB, 0x01, 0xFF])
MAC = "BC:24:11:AB:01:FF"
CT = throwaway.Request(kind="lxc", node="clovis", cores=2, memory=1024, disk_gib=8, vlan=40)
VM = throwaway.Request(kind="qemu", node="clovis", cores=2, memory=2048, disk_gib=16)
REPLICATED = [{"guest": 900, "target": "ace", "id": "900-0"}]
PURGE = "--purge 1 --destroy-unreferenced-disks 1"
READS = {live.RESOURCES, live.HA_RESOURCES, live.HA_RULES, live.REPLICATION}


@pytest.fixture(autouse=True)
def online(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)


@pytest.fixture(autouse=True)
def fresh_default_registry() -> None:
    default_registry.cache_clear()


def write_root(tmp_path: Path, guests: dict | None = None) -> Path:
    def node(name: str, standalone: bool = False) -> dict:
        config = {
            "type": "pve",
            "hostname": f"{name}.example.internal",
            "user": "root",
            "sshkey": "infra",
        }
        if standalone:
            config["standalone"] = True
        return {"config": config, "features": {}}

    data = {name: node(name, standalone) for name, standalone in NODES.items()}
    data["pdm"] = {
        "config": {"type": "ubuntu", "hostname": "p", "user": "root", "sshkey": "infra"},
        "features": {"pve-autoinstall": {"root_ssh_key": KEY}},
    }
    for host, block in (guests or {}).items():
        data[host] = {
            "config": {
                "type": "ubuntu",
                "hostname": f"{host}.example.internal",
                "user": "root",
                "sshkey": "infra",
            },
            "features": {"pve-guest": block},
        }
    (tmp_path / "hosts.conf").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return tmp_path


def registry(root: Path) -> HostRegistry:
    return HostRegistry(root / "hosts.conf")


class Cluster:
    """Scripted PVE nodes: the fixture cluster plus `extra` resources, the rest logged."""

    def __init__(
        self,
        extra: list[dict] | None = None,
        ha: list[dict] | None = None,
        replication: list[dict] | None = None,
        protection: int = 0,
        addresses: list | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.extra = extra or []
        self.ha = ha
        self.replication = replication
        self.protection = protection
        self.addresses = list(addresses or [])

    def _site_read(self, node: str, command: str) -> str:
        site = "osiris" if node == "osiris" else "cluster"
        name = {
            live.RESOURCES: "resources.json",
            live.HA_RESOURCES: "ha_resources.json",
            live.HA_RULES: "ha_rules.json",
            live.REPLICATION: "replication.json",
        }[command]
        payload = json.loads((FIXTURES / site / name).read_text(encoding="utf-8"))
        if command == live.RESOURCES:
            payload += [
                item for item in self.extra if (item["node"] == "osiris") == (site == "osiris")
            ]
        elif command == live.HA_RESOURCES and self.ha is not None:
            payload = self.ha
        elif command == live.REPLICATION and self.replication is not None:
            payload = self.replication
        return json.dumps(payload)

    def __call__(self, node: str, command: str) -> str:
        self.calls.append((node, command))
        if command in READS:
            return self._site_read(node, command)
        if command.endswith("/config --output-format json"):
            return json.dumps({"protection": self.protection})
        if "interfaces --output-format json" in command:
            answer = self.addresses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer)
        if command == INDEX_READ:
            return INDEX
        if command.startswith("curl "):
            return SUMS
        if command.startswith("pveam available"):
            return "system  debian-13-standard_13.6-1_amd64.tar.zst  amd64\n"
        if command.startswith("qm guest exec"):
            return json.dumps({"exitcode": 0, "exited": 1})
        return ""

    def writes(self) -> list[str]:
        return [
            cmd
            for _, cmd in self.calls
            if cmd not in READS and not cmd.endswith(" ping") and "--output-format json" not in cmd
        ]


class FakeClock(create.Clock):
    def __init__(self) -> None:
        self.t = 0.0
        super().__init__(sleep=self._sleep, now=lambda: self.t)

    def _sleep(self, seconds: float) -> None:
        self.t += seconds


def guest(vmid: int, node: str = "clovis", tags: str | None = "throwaway", **rest) -> dict:
    item = {"vmid": vmid, "node": node, "type": "lxc", "name": f"tmp-{vmid}", "status": "running"}
    if tags is not None:
        item["tags"] = tags
    return item | rest


def lxc_interfaces(ip: str | None) -> list[dict]:
    addresses = [{"ip-address": "fe80::1", "ip-address-type": "inet6", "prefix": "64"}]
    if ip:
        addresses.insert(0, {"ip-address": ip, "ip-address-type": "inet", "prefix": "24"})
    return [
        {"hardware-address": "00:00:00:00:00:00", "ip-addresses": [
            {"ip-address": "127.0.0.1", "ip-address-type": "inet", "prefix": "8"}]},
        {"hardware-address": MAC.lower(), "ip-addresses": addresses, "name": "nic0"},
    ]  # fmt: skip


# --------------------------------------------------------------------------------------
# the made-up spec
# --------------------------------------------------------------------------------------


def test_lxc_block_builds_a_bare_tagged_ct_that_does_not_start_on_boot() -> None:
    spec = parse_guest("tmp-900", throwaway.block(CT, 900, MAC), NODES)
    assert build.lxc_create(spec, "local:vztmpl/t.tar.zst") == [
        "pct", "create", "900", "local:vztmpl/t.tar.zst",
        "--arch", "amd64", "--ostype", "debian", "--unprivileged", "1", "--onboot", "0",
        "--swap", "0", "--features", "nesting=1",
        "--cores", "2", "--memory", "1024", "--hostname", "tmp-900", "--tags", "throwaway",
        "--ssh-public-keys", "/tmp/homelab-pve-guest-900.pub",
        "--rootfs", "vm-flash:8",
        "--net0", f"name=nic0,bridge=vmbr0,firewall=1,hwaddr={MAC},ip=dhcp,tag=40",
    ]  # fmt: skip
    assert build.protect(spec) is None


def test_vm_block_has_one_os_disk_and_no_vlan_when_none_is_asked() -> None:
    spec = parse_guest("tmp-901", throwaway.block(VM, 901, MAC), NODES)
    args = build.vm_create(spec, "local:import/i")
    assert args[args.index("--onboot") + 1] == "0"
    assert args[args.index("--tags") + 1] == "throwaway"
    assert [disk.key for disk in spec.disks] == ["scsi0"]
    assert spec.nets[0].tag is None
    assert (spec.profile, spec.ha, spec.replication) == ("linux-vm", None, None)


def test_profile_can_be_chosen() -> None:
    docker = throwaway.Request(**{**CT.__dict__, "profile": "docker-ct"})
    spec = parse_guest("t1", throwaway.block(docker, 900, MAC), NODES)
    assert spec.scalars["features"] == "keyctl=1,nesting=1"


def test_random_mac_is_a_proxmox_mac() -> None:
    assert throwaway.random_mac(lambda n: MAC_BYTES[:n]) == MAC
    assert MAC_RE.fullmatch(throwaway.random_mac())


def test_free_vmid_is_the_lowest_unused_in_the_range() -> None:
    assert throwaway.free_vmid(set()) == 900
    assert throwaway.free_vmid({101, 900, 901}) == 902
    assert throwaway.free_vmid(set(range(900, 999))) == 999
    with pytest.raises(create.CreateError, match="no free throwaway vmid in 900-999"):
        throwaway.free_vmid(set(THROWAWAY_VMIDS))


def test_guest_name(tmp_path: Path) -> None:
    reg = registry(write_root(tmp_path))
    assert throwaway.guest_name(reg, None, 905) == "tmp-905"
    assert throwaway.guest_name(reg, "scratch-1", 905) == "scratch-1"
    for bad in ("Scratch", "1x", "x", "a_b", "trailing-"):
        with pytest.raises(ValueError, match="must be a lower-case DNS label"):
            throwaway.guest_name(reg, bad, 905)
    with pytest.raises(ValueError, match="clovis is a hosts.conf host"):
        throwaway.guest_name(reg, "clovis", 905)


def test_node_domain(tmp_path: Path) -> None:
    reg = registry(write_root(tmp_path))
    assert throwaway.node_domain(reg, "clovis") == "example.internal"
    data = yaml.safe_load((tmp_path / "hosts.conf").read_text())
    data["ace"]["config"]["hostname"] = "ace"
    (tmp_path / "hosts.conf").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="ace: config.hostname has no domain"):
        throwaway.node_domain(HostRegistry(tmp_path / "hosts.conf"), "ace")


def test_plan_skips_vmids_used_on_the_cluster_and_on_standalone_nodes(tmp_path: Path) -> None:
    reg = registry(write_root(tmp_path))
    cluster = Cluster(extra=[guest(900, tags=None), guest(901, node="osiris", tags=None)])
    target = throwaway.plan(reg, CT, cluster, lambda n: MAC_BYTES[:n])
    assert (target.spec.vmid, target.spec.host, target.fqdn) == (
        902,
        "tmp-902",
        "tmp-902.example.internal",
    )
    assert (target.root_key, target.spec.nets[0].mac) == (KEY, MAC)
    asked = {node for node, _ in cluster.calls}
    assert asked == {"clovis", "osiris"}


def test_plan_refusals(tmp_path: Path) -> None:
    reg = registry(write_root(tmp_path))
    with pytest.raises(ValueError, match="nope is not a PVE node"):
        throwaway.plan(reg, throwaway.Request(**{**CT.__dict__, "node": "nope"}), Cluster())
    windows = throwaway.Request(**{**VM.__dict__, "profile": "windows-vm"})
    with pytest.raises(ValueError, match="profile windows-vm is not built by guest create"):
        throwaway.plan(reg, windows, Cluster())
    wrong_kind = throwaway.Request(**{**CT.__dict__, "profile": "linux-vm"})
    with pytest.raises(ValueError, match="profile linux-vm is for qemu guests"):
        throwaway.plan(reg, wrong_kind, Cluster())


def test_hosts_conf_may_not_use_the_throwaway_range(tmp_path: Path) -> None:
    def block(vmid: int) -> dict:
        return throwaway.block(CT, vmid, MAC) | {"onboot": True, "extra": None}

    for vmid in (899, 1000):
        (tmp_path / str(vmid)).mkdir()
        assert list(load_guests(registry(write_root(tmp_path / str(vmid), {"g": block(vmid)}))))
    for vmid in (900, 999):
        (tmp_path / str(vmid)).mkdir()
        root = write_root(tmp_path / str(vmid), {"g": block(vmid)})
        with pytest.raises(ValueError, match=f"g: vmid {vmid} is in the throwaway range 900-999"):
            load_guests(registry(root))


# --------------------------------------------------------------------------------------
# create: the run and the address report
# --------------------------------------------------------------------------------------


def test_ipv4_for_reads_both_interface_shapes() -> None:
    assert throwaway.ipv4_for(lxc_interfaces("10.0.40.50"), MAC) == "10.0.40.50"
    assert throwaway.ipv4_for(lxc_interfaces(None), MAC) is None
    qemu = {
        "result": [
            {"hardware-address": MAC.lower(), "ip-addresses": [
                {"ip-address": "fd00::5", "ip-address-type": "ipv6"},
                {"ip-address": "10.0.40.51", "ip-address-type": "ipv4"}]},
        ]
    }  # fmt: skip
    assert throwaway.ipv4_for(qemu, MAC) == "10.0.40.51"
    assert throwaway.ipv4_for(qemu, "BC:24:11:00:00:00") is None
    assert throwaway.ipv4_for({}, MAC) is None
    assert throwaway.ipv4_for([{"hardware-address": MAC.lower()}], MAC) is None


def test_interfaces_command_per_kind() -> None:
    ct = parse_guest("t1", throwaway.block(CT, 900, MAC), NODES)
    vm = parse_guest("t1", throwaway.block(VM, 900, MAC), NODES)
    assert throwaway.interfaces_command(ct) == (
        "pvesh get /nodes/clovis/lxc/900/interfaces --output-format json"
    )
    assert throwaway.interfaces_command(vm) == (
        "pvesh get /nodes/clovis/qemu/900/agent/network-get-interfaces --output-format json"
    )


def test_lxc_throwaway_builds_then_reports_its_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oks: list[str] = []
    monkeypatch.setattr(throwaway, "print_ok", oks.append)
    cluster = Cluster(
        addresses=[
            live.NodeUnreachable("not yet"),
            lxc_interfaces(None),
            lxc_interfaces("10.0.40.50"),
        ]
    )
    clock = FakeClock()
    throwaway.create_throwaway(
        write_root(tmp_path), CT, False, cluster, clock, lambda n: MAC_BYTES[:n]
    )
    writes = cluster.writes()
    assert any(cmd.startswith("pct create 900 ") and "--tags throwaway" in cmd for cmd in writes)
    assert writes[-2:] == ["pct start 900", "rm -f /tmp/homelab-pve-guest-900.pub"]
    assert clock.t == 2 * throwaway.POLL_SECONDS
    assert oks[-1] == "lxc 900 (tmp-900) on clovis: ssh root@10.0.40.50"


def test_no_address_by_the_deadline_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warns: list[str] = []
    monkeypatch.setattr(throwaway, "print_warn", warns.append)
    polls = throwaway.ADDRESS_TIMEOUT // throwaway.POLL_SECONDS + 1
    cluster = Cluster(addresses=[lxc_interfaces(None)] * polls)
    clock = FakeClock()
    throwaway.create_throwaway(
        write_root(tmp_path), CT, False, cluster, clock, lambda n: MAC_BYTES[:n]
    )
    assert clock.t == throwaway.ADDRESS_TIMEOUT
    assert cluster.addresses == []
    assert warns == ["lxc 900 (tmp-900) on clovis is up but has no DHCP address after 120s"]


def test_dry_run_builds_nothing_and_polls_nothing(tmp_path: Path) -> None:
    cluster = Cluster()
    throwaway.create_throwaway(write_root(tmp_path), VM, True, cluster, FakeClock())
    assert [cmd.split()[0] for cmd in cluster.writes()] == ["curl", "curl"]
    assert not any("interfaces" in cmd for _, cmd in cluster.calls)


def test_offline_mode_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")
    with pytest.raises(ValueError, match="needs the PVE nodes"):
        throwaway.create_throwaway(write_root(tmp_path), CT, True, Cluster(), FakeClock())
    with pytest.raises(ValueError, match="needs the PVE nodes"):
        throwaway.destroy_throwaway(write_root(tmp_path), 900, True, Cluster())


# --------------------------------------------------------------------------------------
# list and destroy
# --------------------------------------------------------------------------------------


def test_is_throwaway_matches_the_whole_tag() -> None:
    assert throwaway.is_throwaway({"tags": "throwaway"})
    assert throwaway.is_throwaway({"tags": "lab;throwaway"})
    assert not throwaway.is_throwaway({"tags": "throwaway-not"})
    assert not throwaway.is_throwaway({})


def test_list_shows_tagged_guests_on_every_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(throwaway, "print_sub", lines.append)
    extra = [guest(903, node="osiris"), guest(900, status="stopped"), guest(901, tags="other")]
    throwaway.list_throwaways(write_root(tmp_path), Cluster(extra=extra))
    assert lines == [
        "900  lxc  tmp-900  on clovis  stopped",
        "903  lxc  tmp-903  on osiris  running",
    ]


def test_list_with_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oks: list[str] = []
    monkeypatch.setattr(throwaway, "print_ok", oks.append)
    throwaway.list_throwaways(write_root(tmp_path), Cluster())
    assert oks == ["no throwaway guests"]


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        ("running", "lxc", ["pct stop 900", f"pct destroy 900 {PURGE}"]),
        ("stopped", "qemu", [f"qm destroy 900 {PURGE}"]),
    ],
)
def test_destroy_runs_on_the_guests_node(
    tmp_path: Path, status: str, kind: str, expected: list[str]
) -> None:
    cluster = Cluster(extra=[guest(900, node="bray", status=status, type=kind)])
    throwaway.destroy_throwaway(write_root(tmp_path), 900, False, cluster)
    assert cluster.writes() == expected
    assert {node for node, cmd in cluster.calls if cmd in expected} == {"bray"}


def test_destroy_dry_run_changes_nothing(tmp_path: Path) -> None:
    cluster = Cluster(extra=[guest(900)])
    throwaway.destroy_throwaway(write_root(tmp_path), 900, True, cluster)
    assert cluster.writes() == []


def test_destroy_finds_a_standalone_throwaway(tmp_path: Path) -> None:
    cluster = Cluster(extra=[guest(950, node="osiris", status="stopped")])
    throwaway.destroy_throwaway(write_root(tmp_path), 950, False, cluster)
    assert cluster.calls[-1] == (
        "osiris",
        f"pct destroy 950 {PURGE}",
    )


@pytest.mark.parametrize(
    ("cluster", "vmid", "message"),
    [
        (Cluster(), 900, "no guest 900 on any PVE node"),
        (Cluster(extra=[guest(900, tags="lab")]), 900, "900: it has no throwaway tag"),
        (Cluster(), 104, "104: it is outside the throwaway range 900-999; it has no throwaway tag"),
        (Cluster(extra=[guest(900)], protection=1), 900, "900: protection is on"),
        (
            Cluster(extra=[guest(900)], ha=[{"sid": "ct:900"}]),
            900,
            "900: it is HA-managed",
        ),
        (
            Cluster(extra=[guest(900)], replication=REPLICATED),
            900,
            "900: it is replicated",
        ),
    ],
)  # fmt: skip
def test_destroy_refuses_anything_but_a_throwaway(
    tmp_path: Path, cluster: Cluster, vmid: int, message: str
) -> None:
    with pytest.raises(create.CreateError, match=message):
        throwaway.destroy_throwaway(write_root(tmp_path), vmid, False, cluster)
    assert cluster.writes() == []


def test_destroy_refuses_a_declared_guest_even_when_tagged(tmp_path: Path) -> None:
    block = throwaway.block(VM, 112, MAC) | {"onboot": True, "extra": None}
    cluster = Cluster(extra=[guest(112, tags="throwaway", type="qemu")])
    root = write_root(tmp_path, {"helm": block})
    with pytest.raises(create.CreateError, match="it is helm in hosts.conf; it is outside"):
        throwaway.destroy_throwaway(root, 112, False, cluster)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_cli_maps_vm_to_qemu_and_passes_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    monkeypatch.setattr(
        throwaway, "create_throwaway", lambda root, req, dry: seen.append((req, dry))
    )
    result = CliRunner().invoke(
        cli.main,
        ["guest", "throwaway", "create", "--node", "ace", "--name", "lab-1", "--dry-run", "vm"],
    )
    assert result.exit_code == 0, result.output
    assert seen == [
        (
            throwaway.Request(
                kind="qemu", node="ace", cores=2, memory=2048, disk_gib=16, vlan=40, name="lab-1"
            ),
            True,
        )
    ]


def test_cli_destroy_asks_first(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    monkeypatch.setattr(throwaway, "destroy_throwaway", lambda root, vmid, dry: seen.append(vmid))
    result = CliRunner().invoke(cli.main, ["guest", "throwaway", "destroy", "900"], input="n\n")
    assert result.exit_code == 1
    assert seen == []
    result = CliRunner().invoke(cli.main, ["guest", "throwaway", "destroy", "--yes", "900"])
    assert result.exit_code == 0, result.output
    assert seen == [900]


def test_cli_turns_refusals_into_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(root, vmid, dry):
        raise create.CreateError("refusing to destroy 104: it has no throwaway tag")

    monkeypatch.setattr(throwaway, "destroy_throwaway", refuse)
    result = CliRunner().invoke(cli.main, ["guest", "throwaway", "destroy", "--dry-run", "104"])
    assert result.exit_code == 1
    assert "refusing to destroy 104" in result.output
