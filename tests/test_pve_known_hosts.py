"""`homelab-pve-known-hosts` (freender/homelab-ops#280).

Each node publishes its own host keys into pmxcfs; every node renders all of them
into the system known_hosts. pmxcfs, /etc/ssh and the hostname are all faked.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "pve-postinstall"
    / "configs"
    / "pve"
    / "homelab-pve-known-hosts"
)

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQRSAKEY"
ECDSA = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAECDSAKEY"


def load_script():
    loader = importlib.machinery.SourceFileLoader("pve_known_hosts", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Node:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "ace") -> None:
        self.script = load_script()
        self.nodes = tmp_path / "etc/pve/nodes"
        self.ssh = tmp_path / "etc/ssh"
        self.conf = tmp_path / "etc/homelab/pve-known-hosts.conf"
        self.known_hosts = self.ssh / "ssh_known_hosts"
        for node in ("ace", "bray", "clovis"):
            (self.nodes / node).mkdir(parents=True)
        self.ssh.mkdir(parents=True)
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(
            "ace ace.example.internal 10.0.0.20\n"
            "bray bray.example.internal 10.0.0.40\n"
            "clovis clovis.example.internal 10.0.0.60\n",
            encoding="utf-8",
        )
        for kind, key in (("ed25519", ED), ("rsa", RSA), ("ecdsa", ECDSA)):
            pub = self.ssh / f"ssh_host_{kind}_key.pub"
            pub.write_text(f"{key} root@{name}\n", encoding="utf-8")
        monkeypatch.setattr(self.script, "NODES_DIR", self.nodes)
        monkeypatch.setattr(self.script, "HOST_KEY_DIR", self.ssh)
        monkeypatch.setattr(self.script, "NAMES_CONF", self.conf)
        monkeypatch.setattr(self.script, "KNOWN_HOSTS", self.known_hosts)
        monkeypatch.setattr(self.script.socket, "gethostname", lambda: f"{name}.example.internal")
        self.name = name

    def published(self, node: str) -> Path:
        return self.nodes / node / self.script.PUBLISHED

    def main(self) -> int:
        return self.script.main()


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Node:
    return Node(tmp_path, monkeypatch)


def test_publishes_every_key_type_without_comments(node: Node) -> None:
    assert node.main() == 0

    assert node.published("ace").read_text(encoding="utf-8") == f"{ECDSA}\n{ED}\n{RSA}\n"


def test_renders_every_published_node_under_all_its_names(node: Node) -> None:
    node.published("bray").write_text(f"{ED}BRAY\n", encoding="utf-8")

    assert node.main() == 0

    lines = node.known_hosts.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# Managed by homelab")
    assert lines[1:] == [
        f"ace,ace.example.internal,10.0.0.20 {ECDSA}",
        f"ace,ace.example.internal,10.0.0.20 {ED}",
        f"ace,ace.example.internal,10.0.0.20 {RSA}",
        f"bray,bray.example.internal,10.0.0.40 {ED}BRAY",
    ]
    assert node.known_hosts.stat().st_mode & 0o777 == 0o644


def test_node_missing_from_the_name_map_is_listed_by_short_name(node: Node) -> None:
    (node.nodes / "osiris").mkdir()
    (node.nodes / "osiris" / node.script.PUBLISHED).write_text(f"{ED}\n", encoding="utf-8")

    node.main()

    assert f"osiris {ED}" in node.known_hosts.read_text(encoding="utf-8").splitlines()


def test_unparseable_published_lines_are_skipped(node: Node) -> None:
    node.published("bray").write_text("garbage\n\nssh-dss\n", encoding="utf-8")

    node.main()

    assert "bray" not in node.known_hosts.read_text(encoding="utf-8")


def test_converged_node_writes_nothing(node: Node) -> None:
    node.main()
    stamps = {path: path.stat().st_mtime_ns for path in (node.known_hosts, node.published("ace"))}

    assert node.main() == 0

    assert {path: path.stat().st_mtime_ns for path in stamps} == stamps
    assert not list(node.nodes.rglob(".*.tmp"))


def test_removed_node_drops_out_of_known_hosts(node: Node) -> None:
    node.published("bray").write_text(f"{ED}\n", encoding="utf-8")
    node.main()

    node.published("bray").unlink()
    (node.nodes / "bray").rmdir()
    node.main()

    assert "bray" not in node.known_hosts.read_text(encoding="utf-8")


def test_missing_own_node_dir_fails_without_writing(node: Node) -> None:
    (node.nodes / "ace").rmdir()

    assert node.main() == 1

    assert not node.known_hosts.exists()


def test_no_published_keys_leaves_known_hosts_alone(node: Node) -> None:
    node.known_hosts.write_text("keep\n", encoding="utf-8")
    for path in node.ssh.glob("ssh_host_*_key.pub"):
        path.unlink()

    assert node.main() == 1

    assert node.known_hosts.read_text(encoding="utf-8") == "keep\n"


def test_failed_publish_still_renders_peers_and_fails(node: Node, capsys) -> None:
    node.published("bray").write_text(f"{ED}\n", encoding="utf-8")
    for path in node.ssh.glob("ssh_host_*_key.pub"):
        path.unlink()

    assert node.main() == 1

    assert "bray" in node.known_hosts.read_text(encoding="utf-8")
    assert "could not publish host keys" in capsys.readouterr().err


def test_name_map_ignores_comments_and_duplicate_names(tmp_path: Path) -> None:
    script = load_script()
    conf = tmp_path / "names.conf"
    conf.write_text("# header\nace ace.example.internal ace  # dup\n\n", encoding="utf-8")

    assert script.read_names(conf) == {"ace": ["ace", "ace.example.internal"]}
