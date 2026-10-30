"""`homelab-pdm-refresh-remote --reconcile` (freender/homelab-ops#282).

PDM pins one pve-ssl.pem fingerprint per node and its PUT replaces the whole node
list, so a re-pin must send every node back. SSH and the PDM API are both faked.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "pve-postinstall-webhook"
    / "scripts"
    / "homelab-pdm-refresh-remote.py"
)

OLD = "AA:" * 31 + "AA"
NEW = "BB:" * 31 + "BB"
KEEP = "CC:" * 31 + "CC"

HOSTS = {
    "ace": {"config": {"type": "pve", "hostname": "ace.example.internal"}},
    "bray": {"config": {"type": "pve", "hostname": "bray.example.internal"}},
    "arc": {"config": {"type": "lxc", "hostname": "10.0.0.50"}},
}


def load_script():
    spec = importlib.util.spec_from_file_location("pdm_refresh_remote", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePdm:
    def __init__(self, remotes: list[dict], version_ok: bool = True) -> None:
        self.remotes = remotes
        self.version_ok = version_ok
        self.puts: list[tuple[str, dict]] = []

    def __call__(self, script, method: str, path: str, body=None):
        if method == "GET" and path == "/api2/json/remotes/remote":
            return self.remotes
        if method == "GET" and path.endswith("/version"):
            if not self.version_ok:
                raise script.PdmApiError(401, "bad fingerprint")
            return {}
        if method == "PUT":
            self.puts.append((path, body))
            return None
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    module = load_script()
    monkeypatch.setattr(module, "load_hosts", lambda: HOSTS)
    return module


def fake_ssh(fingerprints: dict[str, str | None]):
    def run_ssh(target: str, _args: list[str]) -> str:
        fingerprint = fingerprints[target]
        if fingerprint is None:
            raise subprocess.CalledProcessError(255, "ssh", stderr="Host key changed")
        return f"sha256 Fingerprint={fingerprint.lower()}"

    return run_ssh


def wire(monkeypatch, script, pdm: FakePdm, fingerprints: dict[str, str | None]) -> None:
    monkeypatch.setattr(script, "pdm_request", lambda *a, **k: pdm(script, *a, **k))
    monkeypatch.setattr(script, "run_ssh", fake_ssh(fingerprints))


def cluster(*entries: str) -> dict:
    return {"id": "pve-cluster", "type": "pve", "nodes": list(entries)}


def test_one_drifted_node_re_pins_the_whole_node_list(script, monkeypatch) -> None:
    """Sending only the rebuilt node would drop the others from the remote."""
    pdm = FakePdm(
        [
            cluster(
                f"ace.example.internal,fingerprint={OLD}",
                f"bray.example.internal,fingerprint={KEEP}",
            )
        ]
    )
    wire(
        monkeypatch,
        script,
        pdm,
        {"root@ace.example.internal": NEW, "root@bray.example.internal": KEEP},
    )

    script.reconcile(dry_run=False)

    assert pdm.puts == [
        (
            "/api2/json/remotes/remote/pve-cluster",
            {
                "nodes": [
                    f"ace.example.internal,fingerprint={NEW}",
                    f"bray.example.internal,fingerprint={KEEP}",
                ]
            },
        )
    ]


def test_matching_fingerprints_write_nothing(script, monkeypatch) -> None:
    """PDM stores some fingerprints lowercase; case alone is not drift."""
    pdm = FakePdm([cluster(f"ace.example.internal,fingerprint={KEEP.lower()}")])
    wire(monkeypatch, script, pdm, {"root@ace.example.internal": KEEP})

    script.reconcile(dry_run=False)

    assert pdm.puts == []


def test_an_unreachable_node_keeps_its_pin(script, monkeypatch) -> None:
    """Mid-rebuild a node's SSH key has changed or it is down. Its old pin stays,
    and the reachable node that drifted is still corrected."""
    pdm = FakePdm(
        [
            cluster(
                f"ace.example.internal,fingerprint={OLD}",
                f"bray.example.internal,fingerprint={OLD}",
            )
        ]
    )
    wire(
        monkeypatch,
        script,
        pdm,
        {"root@ace.example.internal": None, "root@bray.example.internal": NEW},
    )

    script.reconcile(dry_run=False)

    assert pdm.puts[0][1]["nodes"] == [
        f"ace.example.internal,fingerprint={OLD}",
        f"bray.example.internal,fingerprint={NEW}",
    ]


def test_nodes_missing_from_hosts_conf_and_non_pve_remotes_are_left_alone(
    script, monkeypatch
) -> None:
    pdm = FakePdm(
        [
            cluster(f"stranger.example.internal,fingerprint={OLD}"),
            {"id": "xur", "type": "pbs", "nodes": [f"ace.example.internal,fingerprint={OLD}"]},
        ]
    )
    wire(monkeypatch, script, pdm, {})

    script.reconcile(dry_run=False)

    assert pdm.puts == []


def test_dry_run_reports_drift_without_writing(script, monkeypatch, capsys) -> None:
    pdm = FakePdm([cluster(f"ace.example.internal,fingerprint={OLD}")])
    wire(monkeypatch, script, pdm, {"root@ace.example.internal": NEW})

    script.reconcile(dry_run=True)

    assert pdm.puts == []
    assert "would re-pin pve-cluster: ace.example.internal" in capsys.readouterr().out


def test_a_re_pin_that_still_fails_its_version_check_exits_non_zero(
    script, monkeypatch
) -> None:
    """A clean exit here would leave the unit green while PDM keeps failing."""
    pdm = FakePdm([cluster(f"ace.example.internal,fingerprint={OLD}")], version_ok=False)
    wire(monkeypatch, script, pdm, {"root@ace.example.internal": NEW})

    with pytest.raises(SystemExit):
        script.reconcile(dry_run=False)


def test_node_entry_without_a_fingerprint_parses_as_unpinned(script) -> None:
    assert script.parse_node_entry("ace.example.internal") == ("ace.example.internal", "")
    assert script.parse_node_entry(f"ace.example.internal,fingerprint={OLD}") == (
        "ace.example.internal",
        OLD,
    )
