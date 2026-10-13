"""pve-guest create: the command lines and cloud-init documents, and the run order.

The two golden argv tests are the exact `qm create` / `pct create` lines that built
throwaway VM 900 and CT 901 on clovis (2026-10-13, homelab-ops#171): each came up
in sync with its spec and took root's infra key. A change to either is a change
to what PVE is asked for, so it should be deliberate.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from homelab.hosts import HostRegistry
from homelab.modules.pve_guest import build, create, live
from homelab.modules.pve_guest.spec import parse_guest

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = ROOT / "tests" / "fixtures" / "pve_guest" / "cluster"
OSIRIS = ROOT / "tests" / "fixtures" / "pve_guest" / "osiris"
NODES = {"ace": False, "bray": False, "clovis": False, "osiris": True}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest infra"
DIGEST = "ab" * 64

VM_BLOCK = {
    "vmid": 900,
    "kind": "qemu",
    "home_node": "clovis",
    "profile": "linux-vm",
    "cores": 2,
    "memory": 2048,
    "disks": {"scsi0": {"role": "os", "size": "8G"}, "scsi1": {"role": "scratch", "size": "4G"}},
    "net": {"net0": {"name": "nic0", "mac": "BC:24:11:9F:71:01", "tag": 40}},
}
CT_BLOCK = {
    "vmid": 901,
    "kind": "lxc",
    "home_node": "clovis",
    "profile": "docker-ct",
    "cores": 2,
    "memory": 1024,
    "disks": {
        "rootfs": {"role": "os", "size": "8G"},
        "mp0": {"role": "appdata", "size": "2G"},
        "mp1": {"role": "data", "mount": "/mnt/cache/stuff"},
    },
    "net": {"net0": {"name": "nic0", "mac": "BC:24:11:9F:71:02", "tag": 40}},
}


@pytest.fixture(autouse=True)
def online(monkeypatch: pytest.MonkeyPatch) -> None:
    """`./validate` runs the suite offline; create refuses that, so opt back in."""
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)


def spec_of(block: dict, host: str = "wftest"):
    return parse_guest(host, block, NODES)


# --------------------------------------------------------------------------------------
# build: pure
# --------------------------------------------------------------------------------------


def test_vm_create_is_the_line_that_built_vm_900() -> None:
    spec = spec_of(VM_BLOCK)
    image = f"local:import/{build.image_file(900)}"
    assert build.vm_create(spec, image) == [
        "qm", "create", "900",
        "--machine", "q35", "--bios", "ovmf", "--cpu", "x86-64-v3", "--sockets", "1",
        "--numa", "0", "--scsihw", "virtio-scsi-single", "--ostype", "l26",
        "--agent", "enabled=1", "--tablet", "0", "--onboot", "1",
        "--cores", "2", "--memory", "2048", "--name", "wftest", "--boot", "order=scsi0",
        "--efidisk0", "vm-flash:1,efitype=4m,pre-enrolled-keys=0",
        "--scsi0",
        "vm-flash:0,import-from=local:import/homelab-900-debian-13-generic-amd64.qcow2,"
        "discard=on,iothread=1,ssd=1",
        "--scsi1", "vm-flash:4,backup=0,discard=on,iothread=1,ssd=1",
        "--net0", "virtio=BC:24:11:9F:71:01,bridge=vmbr0,firewall=1,tag=40",
        "--ide2", "local:cloudinit",
        "--cicustom",
        "user=local:snippets/homelab-900-user.yaml,network=local:snippets/homelab-900-network.yaml",
    ]  # fmt: skip
    assert build.vm_resize(spec) == ["qm", "disk", "resize", "900", "scsi0", "8G"]


def test_lxc_create_is_the_line_that_built_ct_901() -> None:
    spec = spec_of(CT_BLOCK)
    assert build.lxc_create(spec, "local:vztmpl/t.tar.zst") == [
        "pct", "create", "901", "local:vztmpl/t.tar.zst",
        "--arch", "amd64", "--ostype", "debian", "--unprivileged", "1", "--onboot", "1",
        "--swap", "0", "--features", "keyctl=1,nesting=1",
        "--cores", "2", "--memory", "1024", "--hostname", "wftest",
        "--ssh-public-keys", "/tmp/homelab-pve-guest-901.pub",
        "--rootfs", "vm-flash:8",
        "--mp0", "vm-flash:2,mp=/mnt/cache/appdata,backup=1",
        "--mp1", "vm-flash:0,mp=/mnt/cache/stuff,backup=1",
        "--net0", "name=nic0,bridge=vmbr0,firewall=1,hwaddr=BC:24:11:9F:71:02,ip=dhcp,tag=40",
    ]  # fmt: skip


def test_disk_flags_are_written_only_where_pve_would_assume_otherwise() -> None:
    block = {
        **CT_BLOCK,
        "disks": {
            "rootfs": {"role": "os", "size": "8G", "backup": False},
            "mp0": {"role": "scratch", "size": "1G", "replicate": False},
            "mp1": {"role": "data", "source": "/tank/x", "mount": "/x", "shared": True},
        },
        "net": {
            "net0": {
                "name": "nic0",
                "mac": "BC:24:11:00:00:01",
                "firewall": False,
                "ip": "10.0.40.9/24",
                "gw": "10.0.40.1",
            }
        },
    }
    spec = spec_of(block)
    disks = {disk.key: build.lxc_disk(spec, disk) for disk in spec.disks}
    assert disks == {
        "rootfs": "vm-flash:8,backup=0",
        "mp0": "vm-flash:1,mp=/mnt/scratch,replicate=0",
        "mp1": "/tank/x,mp=/x,backup=1,shared=1",
    }
    assert build.lxc_net(spec.nets[0]) == (
        "name=nic0,bridge=vmbr0,firewall=0,hwaddr=BC:24:11:00:00:01,ip=10.0.40.9/24,gw=10.0.40.1"
    )

    vm = spec_of(
        {
            **VM_BLOCK,
            "disks": {
                "scsi0": {"role": "os", "size": "8G", "backup": False},
                "scsi1": {"role": "data", "size": "2G", "mount": "/d", "replicate": False},
            },
            "net": {"net0": {"name": "nic0", "mac": "BC:24:11:00:00:01", "firewall": False}},
        }
    )
    vm_disks = {disk.key: build.vm_disk(vm, disk, "local:import/i") for disk in vm.disks}
    assert vm_disks == {
        "scsi0": "vm-flash:0,import-from=local:import/i,backup=0,discard=on,iothread=1,ssd=1",
        "scsi1": "vm-flash:2,replicate=0,discard=on,iothread=1,ssd=1",
    }
    assert build.vm_net(vm, vm.nets[0]) == "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=0"


def test_sizes_must_be_whole_gib() -> None:
    spec = spec_of({**VM_BLOCK, "disks": {"scsi0": {"role": "os", "size": "1536M"}}})
    with pytest.raises(ValueError, match=r"wftest: pve-guest\.disks\.scsi0\.size must be whole"):
        build.vm_resize(spec)
    ct = spec_of(CT_BLOCK)
    assert [build.gib(ct, disk) for disk in ct.disks] == [8, 2, 0]


def test_cloud_init_documents() -> None:
    block = {
        **VM_BLOCK,
        "net": {
            "net0": {"name": "nic0", "mac": "BC:24:11:9F:71:01"},
            "net1": {"name": "nic1", "mac": "BC:24:11:9F:71:0A", "tag": 10},
        },
    }
    spec = spec_of(block)
    user = build.user_data(spec, KEY, "wftest.example.internal")
    assert user.startswith("#cloud-config\n")
    assert yaml.safe_load(user) == {
        "hostname": "wftest",
        "fqdn": "wftest.example.internal",
        "manage_etc_hosts": True,
        "users": [],
        "disable_root": False,
        "ssh_authorized_keys": [KEY],
        "package_update": True,
        "packages": ["qemu-guest-agent"],
        "runcmd": [["systemctl", "start", "qemu-guest-agent"]],
    }
    assert yaml.safe_load(build.network_config(spec)) == {
        "network": {
            "version": 2,
            "ethernets": {
                name: {
                    "match": {"macaddress": mac},
                    "set-name": name,
                    "dhcp4": True,
                    "dhcp-identifier": "mac",
                }
                for name, mac in (("nic0", "bc:24:11:9f:71:01"), ("nic1", "bc:24:11:9f:71:0a"))
            },
        }
    }


SUMS = f"""\
{"cd" * 64}  debian-13-genericcloud-amd64.qcow2
{DIGEST}  debian-13-generic-amd64.qcow2
{"ef" * 64}  debian-13-generic-arm64.qcow2
"""


def test_image_checksum_picks_the_exact_file() -> None:
    assert build.image_checksum(SUMS) == DIGEST
    with pytest.raises(ValueError, match="lists no debian-13-generic-amd64.qcow2"):
        build.image_checksum(SUMS.replace("generic-amd64", "generic-amd65"))
    with pytest.raises(ValueError, match="malformed digest"):
        build.image_checksum(f"{'AB' * 64}  debian-13-generic-amd64.qcow2\n")


def test_newest_template_compares_versions_as_numbers() -> None:
    # The real `pveam available --section system` shape: section, template, arch.
    available = """\
system          debian-12-standard_12.12-1_amd64.tar.zst                     amd64
system          debian-13-standard_13.6-1_amd64.tar.zst                      amd64
system          debian-13-standard_13.6-1_arm64.tar.zst                      arm64
"""
    assert build.newest_template(available) == "debian-13-standard_13.6-1_amd64.tar.zst"
    newer = available + (
        "system   debian-13-standard_13.10-1_amd64.tar.zst   amd64\n"
        "system   debian-13-standard_13.6-2_amd64.tar.zst   amd64\n"
        "\n"
    )
    assert build.newest_template(newer) == "debian-13-standard_13.10-1_amd64.tar.zst"
    with pytest.raises(ValueError, match="no amd64 debian-13-standard"):
        build.newest_template(available.replace("_amd64", "_x"))


def test_write_file_round_trips_any_content(tmp_path: Path) -> None:
    content = "#cloud-config\nkey: 'it''s' \"$HOME\" `x` \\n\n"
    target = tmp_path / "out file"
    subprocess.run(["bash", "-c", build.write_file(str(target), content)], check=True)
    assert target.read_text() == content


def test_scratch_paths_are_what_a_create_leaves_behind() -> None:
    assert build.scratch_paths(spec_of(VM_BLOCK)) == [
        "/var/lib/vz/import/homelab-900-debian-13-generic-amd64.qcow2",
        "/var/lib/vz/snippets/homelab-900-user.yaml",
        "/var/lib/vz/snippets/homelab-900-network.yaml",
    ]
    assert build.scratch_paths(spec_of(CT_BLOCK)) == ["/tmp/homelab-pve-guest-901.pub"]


# --------------------------------------------------------------------------------------
# create: inventory lookups and the run order against a scripted node
# --------------------------------------------------------------------------------------


def write_root(tmp_path: Path, block: dict, keys: tuple[str, ...] = (KEY,), fqdn=None) -> Path:
    def node(standalone: bool = False) -> dict:
        config = {"type": "pve", "hostname": "n", "user": "root", "sshkey": "infra"}
        if standalone:
            config["standalone"] = True
        return {"config": config, "features": {}}

    data = {"ace": node(), "bray": node(), "clovis": node(), "osiris": node(True)}
    for index, key in enumerate(keys):
        data[f"pdm{index}"] = {
            "config": {"type": "ubuntu", "hostname": "p", "user": "root", "sshkey": "infra"},
            "features": {"pve-autoinstall": {"root_ssh_key": key}},
        }
    data["wftest"] = {
        "config": {
            "type": "ubuntu",
            "hostname": fqdn or "wftest.example.internal",
            "user": "root",
            "sshkey": "infra",
        },
        "features": {"pve-guest": block},
    }
    (tmp_path / "hosts.conf").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return tmp_path


def registry(root: Path) -> HostRegistry:
    return HostRegistry(root / "hosts.conf")


def test_infra_key_needs_exactly_one_source(tmp_path: Path) -> None:
    assert create.infra_key(registry(write_root(tmp_path, VM_BLOCK))) == KEY
    with pytest.raises(ValueError, match="found: none"):
        create.infra_key(registry(write_root(tmp_path, VM_BLOCK, keys=())))
    with pytest.raises(ValueError, match="found: pdm0, pdm1"):
        create.infra_key(registry(write_root(tmp_path, VM_BLOCK, keys=(KEY, KEY))))


def test_load_target_needs_a_block_and_an_fqdn(tmp_path: Path) -> None:
    target = create.load_target(registry(write_root(tmp_path, VM_BLOCK)), "wftest")
    assert (target.spec.vmid, target.fqdn, target.root_key) == (
        900,
        "wftest.example.internal",
        KEY,
    )
    with pytest.raises(ValueError, match="config.hostname must be the guest's fqdn"):
        create.load_target(registry(write_root(tmp_path, VM_BLOCK, fqdn="other.x")), "wftest")
    with pytest.raises(ValueError, match="ace has no pve-guest block"):
        create.load_target(registry(write_root(tmp_path, VM_BLOCK)), "ace")


def test_sites_put_the_home_node_first_and_ask_each_standalone_node() -> None:
    assert create.sites(NODES, "clovis") == [["clovis", "ace", "bray"], ["osiris"]]
    assert create.sites(NODES, "osiris") == [["ace", "bray", "clovis"], ["osiris"]]
    assert create.sites({"osiris": True}, "osiris") == [["osiris"]]


class Node:
    """A scripted home node: cluster reads come from the fixtures, the rest is logged."""

    def __init__(self, agent_misses: int = 0, cloud_init: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.agent_misses = agent_misses
        self.cloud_init = cloud_init

    def __call__(self, node: str, command: str) -> str:
        self.calls.append((node, command))
        reads = {
            live.RESOURCES: "resources.json",
            live.HA_RESOURCES: "ha_resources.json",
            live.HA_RULES: "ha_rules.json",
            live.REPLICATION: "replication.json",
        }
        if command in reads:
            site = OSIRIS if node == "osiris" else CLUSTER
            return (site / reads[command]).read_text(encoding="utf-8")
        if command.startswith("curl "):
            return SUMS
        if command.startswith("pveam available"):
            return "system  debian-13-standard_13.6-1_amd64.tar.zst  amd64\n"
        if command.endswith(" ping"):
            if self.agent_misses:
                self.agent_misses -= 1
                raise live.NodeUnreachable("QEMU guest agent is not running")
            return ""
        if "cloud-init status" in command:
            return json.dumps({"exitcode": self.cloud_init, "exited": 1})
        if command.startswith("qm guest exec"):
            return json.dumps({"exitcode": 0, "exited": 1})
        return ""

    def writes(self) -> list[str]:
        """Everything but the cluster reads and the agent pings, in order."""
        skip = {live.RESOURCES, live.HA_RESOURCES, live.HA_RULES, live.REPLICATION}
        return [cmd for _, cmd in self.calls if cmd not in skip and not cmd.endswith(" ping")]


class FakeClock(create.Clock):
    def __init__(self) -> None:
        self.t = 0.0
        super().__init__(sleep=self._sleep, now=lambda: self.t)

    def _sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def no_report(monkeypatch: pytest.MonkeyPatch) -> list:
    reports: list = []
    monkeypatch.setattr(create, "report", lambda runner, target: reports.append(target))
    return reports


def short(cmd: str) -> str:
    return cmd if cmd.startswith(("qm ", "pct ", "pveam ", "rm ")) else cmd.split()[0]


def test_vm_create_runs_in_order_and_cleans_up(tmp_path: Path, no_report: list) -> None:
    node = Node(agent_misses=2)
    clock = FakeClock()
    create.create_guest(write_root(tmp_path, VM_BLOCK), "wftest", False, node, clock)
    steps = [short(cmd) for cmd in node.writes()]
    assert steps[:4] == ["curl", "pvesh", "echo", "echo"]
    assert steps[4].startswith("qm create 900 ")
    assert steps[5:] == [
        "qm disk resize 900 scsi0 8G",
        "qm start 900",
        "qm guest exec 900 --timeout 900 -- cloud-init status --wait",
        "qm guest exec 900 --timeout 60 -- touch /etc/cloud/cloud-init.disabled",
        "qm shutdown 900 --timeout 180",
        "qm set 900 --delete ide2,cicustom",
        "qm start 900",
        "rm -f " + " ".join(build.scratch_paths(spec_of(VM_BLOCK))),
    ]
    reads = {live.RESOURCES, live.HA_RESOURCES, live.HA_RULES, live.REPLICATION}
    assert {called for called, cmd in node.calls if cmd not in reads} == {"clovis"}
    assert clock.t == 2 * create.POLL_SECONDS
    assert len(no_report) == 1


def test_vm_create_refuses_a_failed_cloud_init_and_still_cleans_up(
    tmp_path: Path, no_report: list
) -> None:
    node = Node(cloud_init=1)
    with pytest.raises(create.CreateError, match="cloud-init on 900 ended with status 1"):
        create.create_guest(write_root(tmp_path, VM_BLOCK), "wftest", False, node, FakeClock())
    steps = [short(cmd) for cmd in node.writes()]
    assert steps[-1].startswith("rm -f /var/lib/vz/import/")
    assert "qm shutdown 900 --timeout 180" not in steps
    assert no_report == []


def test_cloud_init_warnings_are_not_a_failure(tmp_path: Path, no_report: list) -> None:
    node = Node(cloud_init=2)
    create.create_guest(write_root(tmp_path, VM_BLOCK), "wftest", False, node, FakeClock())
    assert "qm set 900 --delete ide2,cicustom" in node.writes()


def test_vm_create_gives_up_on_a_silent_agent(tmp_path: Path, no_report: list) -> None:
    node = Node(agent_misses=10**6)
    clock = FakeClock()
    with pytest.raises(create.CreateError, match="no guest agent on 900 after 600s"):
        create.create_guest(write_root(tmp_path, VM_BLOCK), "wftest", False, node, clock)
    assert clock.t == create.AGENT_TIMEOUT
    assert node.writes()[-1].startswith("rm -f ")


def test_lxc_create_runs_in_order_and_cleans_up(tmp_path: Path, no_report: list) -> None:
    node = Node()
    create.create_guest(write_root(tmp_path, CT_BLOCK), "wftest", False, node, FakeClock())
    steps = [short(cmd) for cmd in node.writes()]
    assert steps[:4] == [
        "pveam update",
        "pveam available --section system",
        "pveam download local debian-13-standard_13.6-1_amd64.tar.zst",
        "echo",
    ]
    assert steps[4].startswith(
        "pct create 901 local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst "
    )
    assert steps[5:] == ["pct start 901", "rm -f /tmp/homelab-pve-guest-901.pub"]
    assert (
        KEY
        in subprocess.run(
            ["bash", "-c", node.writes()[3].rsplit(">", 1)[0]], capture_output=True, text=True
        ).stdout
    )


def test_dry_run_only_reads(tmp_path: Path, no_report: list) -> None:
    for block in (VM_BLOCK, CT_BLOCK):
        node = Node()
        create.create_guest(write_root(tmp_path, block), "wftest", True, node, FakeClock())
        assert [short(cmd) for cmd in node.writes()] in (
            ["curl"],
            ["pveam available --section system"],
        )
    assert no_report == []


@pytest.mark.parametrize(("vmid", "where"), [(112, "clovis (qemu helm)"), (105, "osiris")])
def test_an_existing_vmid_anywhere_is_refused(tmp_path: Path, vmid: int, where: str) -> None:
    node = Node()
    root = write_root(tmp_path, {**VM_BLOCK, "vmid": vmid})
    message = re.escape(f"vmid {vmid} already exists on {where}")
    with pytest.raises(create.CreateError, match=message):
        create.create_guest(root, "wftest", False, node, FakeClock())
    assert node.writes() == []


def test_offline_mode_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")
    with pytest.raises(ValueError, match="needs the PVE nodes"):
        create.create_guest(write_root(tmp_path, VM_BLOCK), "wftest", False, Node(), FakeClock())


def test_report_reads_the_new_guest_back_from_its_home_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(create, "print_sub", lines.append)
    block = {**VM_BLOCK, "replication": {"target": "ace"}}
    target = create.load_target(registry(write_root(tmp_path, block)), "wftest")
    node = Node()
    create.report(node, target)
    assert node.calls[0] == ("clovis", live.RESOURCES)
    # The fixture has no guest 900, which is exactly what the drift report then says.
    assert lines[0].startswith("MISSING: no guest 900")
    assert lines[-1] == "replication and HA are not created here; the report above lists them"
