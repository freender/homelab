"""`homelab-known-hosts` on arc (freender/homelab-ops#281).

arc stores each PVE node's host keys, renders them into its system known_hosts
and pushes that file to riven. The store, /etc/ssh and SSH are all faked.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from homelab.modules import pve_postinstall_webhook

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "pve-postinstall-webhook"
    / "scripts"
    / "homelab-known-hosts.py"
)

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEDKEY"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQRSAKEY"
ECDSA = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAECDSAKEY"
OLD_ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOLDKEY"

NAMES = (
    "ace ace.example.internal 10.0.0.20\n"
    "bray bray.example.internal 10.0.0.40\n"
    "osiris osiris.example.internal 10.0.0.80\n"
)
RIVEN = "root@riven.example.internal"


def load_script():
    spec = importlib.util.spec_from_file_location("homelab_known_hosts", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSsh:
    """Answers the host-key read per target and records every push.

    A target mapped to None fails like a changed or unreachable host."""

    def __init__(self, keys: dict[str, str | None], push_fails: bool = False) -> None:
        self.keys = keys
        self.push_fails = push_fails
        self.calls: list[tuple[list[str], str]] = []
        self.pushed: dict[str, str] = {}

    def __call__(self, options, target, command, stdin=None) -> str:
        self.calls.append((list(options), target))
        if stdin is not None:
            if self.push_fails:
                raise subprocess.CalledProcessError(255, "ssh", stderr="Connection refused")
            self.pushed[target] = stdin
            return "updated\n"
        answer = self.keys.get(target)
        if answer is None:
            raise subprocess.CalledProcessError(255, "ssh", stderr="Host key verification failed")
        return answer

    def options_for(self, target: str) -> list[str]:
        return next(options for options, called in self.calls if called == target)


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = load_script()
    script.STORE = tmp_path / "host-keys"
    script.NAMES_CONF = tmp_path / "pve-known-hosts.conf"
    script.CONFIG_FILE = tmp_path / "poller.env"
    script.KNOWN_HOSTS = tmp_path / "ssh_known_hosts"
    script.NAMES_CONF.write_text(NAMES, encoding="utf-8")
    script.CONFIG_FILE.write_text(f"KNOWN_HOSTS_PUSH='{RIVEN}'\n", encoding="utf-8")
    monkeypatch.delenv("KNOWN_HOSTS_PUSH", raising=False)
    return script


def wire(monkeypatch, script, ssh: FakeSsh) -> FakeSsh:
    monkeypatch.setattr(script, "run_ssh", ssh)
    return ssh


def event(tmp_path: Path, keys: dict[str, str] | None) -> Path:
    data: dict = {"answer-id": "ace", "post-hook-data": {"fqdn": "ace.example.internal"}}
    if keys is not None:
        data["post-hook-data"]["ssh-public-host-keys"] = keys
    path = tmp_path / "event.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def rendered(script) -> list[str]:
    return [
        line
        for line in script.KNOWN_HOSTS.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]


def test_record_trusts_the_installer_payload_under_every_name(tool, tmp_path, monkeypatch):
    """The payload is keyed `{type: line}` and carries a comment; every key type
    goes in, under short name, FQDN and mgmt IP, with no SSH to the node."""
    ssh = wire(monkeypatch, tool, FakeSsh({}))
    path = event(
        tmp_path,
        {"ed25519": f"{ED} root@ace", "rsa": f"{RSA} root@ace", "ecdsa": f"{ECDSA} root@ace"},
    )

    assert tool.main(["record", "ace", str(path)]) == 0

    names = "ace,ace.example.internal,10.0.0.20"
    assert rendered(tool) == sorted(f"{names} {key}" for key in (ED, RSA, ECDSA))
    assert [target for _options, target in ssh.calls] == [RIVEN]


def test_record_pushes_the_same_file_to_riven(tool, tmp_path, monkeypatch):
    ssh = wire(monkeypatch, tool, FakeSsh({}))

    tool.main(["record", "ace", str(event(tmp_path, {"ed25519": ED}))])

    assert ssh.pushed[RIVEN] == tool.KNOWN_HOSTS.read_text(encoding="utf-8")


def test_a_payload_without_keys_records_nothing_and_says_so(tool, tmp_path, monkeypatch):
    """Exit 3 is the worker's cue to fall back to first-use capture."""
    ssh = wire(monkeypatch, tool, FakeSsh({}))

    assert tool.main(["record", "ace", str(event(tmp_path, None))]) == tool.NO_KEYS
    assert not tool.KNOWN_HOSTS.exists()
    assert ssh.calls == []


def test_an_unreadable_event_falls_back_too(tool, tmp_path, monkeypatch):
    wire(monkeypatch, tool, FakeSsh({}))
    path = tmp_path / "event.json"
    path.write_text("{not json", encoding="utf-8")

    assert tool.main(["record", "ace", str(path)]) == tool.NO_KEYS


def test_a_rebuild_replaces_the_old_keys(tool, tmp_path, monkeypatch):
    wire(monkeypatch, tool, FakeSsh({}))
    tool.STORE.mkdir()
    (tool.STORE / "ace").write_text(f"{OLD_ED}\n", encoding="utf-8")

    tool.main(["record", "ace", str(event(tmp_path, {"ed25519": ED}))])

    assert rendered(tool) == [f"ace,ace.example.internal,10.0.0.20 {ED}"]


def test_a_failed_push_still_updates_arc_but_reports_failure(tool, tmp_path, monkeypatch):
    """riven down must not stop arc trusting the rebuilt node, but it must show."""
    wire(monkeypatch, tool, FakeSsh({}, push_fails=True))

    status = tool.main(["record", "ace", str(event(tmp_path, {"ed25519": ED}))])

    assert status == 1
    assert rendered(tool) == [f"ace,ace.example.internal,10.0.0.20 {ED}"]


def test_capture_uses_the_workers_first_use_trust(tool, monkeypatch):
    ssh = wire(monkeypatch, tool, FakeSsh({"root@ace.example.internal": f"{ED} root@ace\n"}))

    assert tool.main(["capture", "ace"]) == 0

    options = ssh.options_for("root@ace.example.internal")
    assert "StrictHostKeyChecking=accept-new" in options
    assert "GlobalKnownHostsFile=/dev/null" in options
    assert rendered(tool) == [f"ace,ace.example.internal,10.0.0.20 {ED}"]


def test_capture_replaces_a_stale_stored_key(tool, monkeypatch):
    """A by-hand run after a rebuild: the store still holds the old key, which
    capture must ignore on the way in and replace on the way out."""
    tool.STORE.mkdir()
    (tool.STORE / "ace").write_text(f"{OLD_ED}\n", encoding="utf-8")
    wire(monkeypatch, tool, FakeSsh({"root@ace.example.internal": f"{ED} root@ace\n"}))

    assert tool.main(["capture", "ace"]) == 0

    assert rendered(tool) == [f"ace,ace.example.internal,10.0.0.20 {ED}"]


def test_seed_only_reads_nodes_it_has_no_keys_for_and_only_over_trusted_ssh(tool, monkeypatch):
    """Seeding must never trust a changed key; a stored node is left to the
    rebuild path, and an untrusted one is skipped rather than accepted."""
    tool.STORE.mkdir()
    (tool.STORE / "ace").write_text(f"{ED}\n", encoding="utf-8")
    ssh = wire(
        monkeypatch,
        tool,
        FakeSsh({"root@bray.example.internal": f"{RSA}\n", "root@osiris.example.internal": None}),
    )

    assert tool.main(["seed"]) == 0

    targets = [target for _options, target in ssh.calls]
    assert "root@ace.example.internal" not in targets
    assert "StrictHostKeyChecking=yes" in ssh.options_for("root@bray.example.internal")
    assert "StrictHostKeyChecking=yes" in ssh.options_for("root@osiris.example.internal")
    assert [line.split()[0] for line in rendered(tool)] == [
        "ace,ace.example.internal,10.0.0.20",
        "bray,bray.example.internal,10.0.0.40",
    ]


def test_a_node_dropped_from_inventory_drops_out_of_the_file(tool, monkeypatch):
    wire(monkeypatch, tool, FakeSsh({}))
    tool.STORE.mkdir()
    (tool.STORE / "ace").write_text(f"{ED}\n", encoding="utf-8")
    (tool.STORE / "pxe-test").write_text(f"{RSA}\n", encoding="utf-8")

    tool.main(["sync"])

    assert rendered(tool) == [f"ace,ace.example.internal,10.0.0.20 {ED}"]


def test_an_empty_store_leaves_known_hosts_alone(tool, monkeypatch):
    ssh = wire(monkeypatch, tool, FakeSsh({}))
    tool.KNOWN_HOSTS.write_text("existing\n", encoding="utf-8")

    assert tool.main(["sync"]) == 1

    assert tool.KNOWN_HOSTS.read_text(encoding="utf-8") == "existing\n"
    assert ssh.pushed == {}


class Registry:
    def __init__(self, hosts: dict[str, dict]) -> None:
        self.hosts = hosts

    def list_hosts(self, feature: str | None = None) -> list[str]:
        del feature
        return list(self.hosts)

    def get(self, host: str, key: str, default: object = None) -> object:
        return self.hosts[host].get(key, default)


def test_push_targets_resolve_to_the_deploy_login():
    registry = Registry(
        {
            "arc": {"pve-postinstall-webhook.known_hosts_push": ["riven"]},
            "riven": {"config.user": "root", "config.hostname": "riven.example.internal"},
        }
    )

    assert pve_postinstall_webhook.known_hosts_push_targets(registry, "arc") == RIVEN


def test_an_unknown_push_target_fails_validation():
    registry = Registry({"arc": {"pve-postinstall-webhook.known_hosts_push": ["rivne"]}})

    with pytest.raises(ValueError, match="rivne"):
        pve_postinstall_webhook.known_hosts_push_targets(registry, "arc")


def test_no_push_targets_is_an_empty_list():
    assert pve_postinstall_webhook.known_hosts_push_targets(Registry({"arc": {}}), "arc") == ""
