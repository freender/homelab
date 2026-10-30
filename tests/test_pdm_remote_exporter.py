"""Behavioural tests for metrics-exporters' pdm-remote-textfile-exporter.

The properties pinned here are the ones that decide whether the pdm-remotes.yml
rules can fire, and whether they can fire wrongly:

  - No `host` label; the scrape config owns it. The pinned node is `node_host`.
  - `reason` is a fixed class, never the raw error text.
  - fingerprint_match is published only for a reachable node with a pin, so an
    unreachable node never reads as a mismatch.
  - A pin compares equal regardless of case and colons (PDM stores both forms).
  - The probe reads the certificate actually served, self-signed or not.
  - A file that cannot be parsed exits 1 and leaves the previous .prom alone.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "pdm-remote-textfile-exporter.py"

PIN_A = ":".join(["AA"] * 32)
PIN_B = ":".join(["bb"] * 32)
PIN_C = ":".join(["CC"] * 32)

# Shape of arc's remotes.cfg (PDM 1.1.9), with placeholder names and pins.
REMOTES_CFG = f"""pve: pve-cluster
\tauthid root@pam!pdm
\tnodes ace.example.internal,fingerprint={PIN_A}
\tnodes bray.example.internal,fingerprint={PIN_B}
\ttoken redacted
\tweb-url https://ace.example.net/

pve: osiris
\tauthid root@pam!pdm
\tnodes osiris.example.internal,fingerprint={PIN_C}
\ttoken redacted

pbs: xur
\tauthid root@pam!pdm
\tnodes xur.example.internal,fingerprint={PIN_B.lower()}
\ttoken redacted
"""

STATE = {
    "remote-status": {
        "pve-cluster": {"most-recent-datapoint": 1793395795, "last-collection": 1793395800},
        "osiris": {
            "most-recent-datapoint": 1793395794,
            "last-collection": 1793300000,
            "error": "error trying to connect: bad fingerprint for osiris",
        },
        "xur": {"most-recent-datapoint": 1793395793, "last-collection": 1793395800},
    }
}


CLUSTER_OK = '{remote="pve-cluster",type="pve",reason=""}'


def _load():
    spec = importlib.util.spec_from_file_location("pdm_remote_textfile_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the defining module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return _load()


def series(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = value
    return out


def rendered(exporter, served=None) -> str:
    """Every node serves the certificate it is pinned to unless `served` says otherwise."""
    remotes = exporter.parse_remotes(REMOTES_CFG)
    status = exporter.parse_state(json.dumps(STATE))
    served = served or {}
    probes = {
        node: served.get(node.short, node.fingerprint)
        for remote in remotes
        for node in remote.nodes
    }
    return exporter.render(remotes, status, probes)


def test_remotes_cfg_yields_every_pinned_node_with_its_api_port(exporter):
    remotes = {remote.name: remote for remote in exporter.parse_remotes(REMOTES_CFG)}
    assert [node.address for node in remotes["pve-cluster"].nodes] == [
        "ace.example.internal",
        "bray.example.internal",
    ]
    assert remotes["pve-cluster"].nodes[0].port == 8006
    assert remotes["xur"].kind == "pbs"
    assert remotes["xur"].nodes[0].port == 8007
    assert remotes["osiris"].nodes[0].short == "osiris"


def test_explicit_port_and_ip_address_are_kept(exporter):
    node = exporter.parse_node("10.0.0.80:9006,fingerprint=AA", "pve")
    assert (node.address, node.port, node.short) == ("10.0.0.80", 9006, "10.0.0.80")
    bracketed = exporter.parse_node("[fd00::1]:8006", "pve")
    assert (bracketed.address, bracketed.port, bracketed.fingerprint) == ("fd00::1", 8006, None)


def test_pins_compare_regardless_of_case_and_colons(exporter):
    values = series(rendered(exporter))
    xur = '{remote="xur",node="xur.example.internal",node_host="xur"}'
    assert values[f"homelab_pdm_remote_node_fingerprint_match{xur}"] == "1"


def test_labels_avoid_the_host_label_the_scrape_config_owns(exporter):
    for line in rendered(exporter).splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        block = line[line.index("{") + 1 : line.rindex("}")]
        names = {pair.split("=", 1)[0] for pair in block.split(",")}
        assert "host" not in names
        assert "job" not in names


def test_remote_counts_and_collection_state(exporter):
    values = series(rendered(exporter))
    assert values["homelab_pdm_remotes_total"] == "3"
    cluster = '{remote="pve-cluster",type="pve"}'
    assert values[f"homelab_pdm_remote_last_collection_timestamp_seconds{cluster}"] == "1793395800"
    assert values[f"homelab_pdm_remote_collection_error{CLUSTER_OK}"] == "0"
    assert (
        values['homelab_pdm_remote_collection_error{remote="osiris",type="pve",reason="fingerprint"}']
        == "1"
    )


def test_one_stale_cluster_pin_is_visible_while_the_remote_still_collects(exporter):
    """The case remote-level status misses: the cluster falls back to its other nodes."""
    values = series(rendered(exporter, served={"ace": "00" * 32}))
    ace = '{remote="pve-cluster",node="ace.example.internal",node_host="ace"}'
    bray = '{remote="pve-cluster",node="bray.example.internal",node_host="bray"}'
    assert values[f"homelab_pdm_remote_node_fingerprint_match{ace}"] == "0"
    assert values[f"homelab_pdm_remote_node_fingerprint_match{bray}"] == "1"
    assert values[f"homelab_pdm_remote_collection_error{CLUSTER_OK}"] == "0"


def test_unreachable_node_reports_no_fingerprint_verdict(exporter):
    values = series(rendered(exporter, served={"bray": None}))
    bray = '{remote="pve-cluster",node="bray.example.internal",node_host="bray"}'
    assert values[f"homelab_pdm_remote_node_reachable{bray}"] == "0"
    assert f"homelab_pdm_remote_node_fingerprint_match{bray}" not in values


def test_unpinned_node_reports_reachability_only(exporter):
    remotes = exporter.parse_remotes("pve: lab\n\tnodes lab.example.internal\n")
    values = series(exporter.render(remotes, {}, {remotes[0].nodes[0]: "ab" * 32}))
    lab = '{remote="lab",node="lab.example.internal",node_host="lab"}'
    assert values[f"homelab_pdm_remote_node_reachable{lab}"] == "1"
    assert f"homelab_pdm_remote_node_fingerprint_match{lab}" not in values


def test_remote_missing_from_state_has_no_timestamp_and_no_error(exporter):
    remotes = exporter.parse_remotes("pve: lab\n\tnodes lab.example.internal\n")
    values = series(exporter.render(remotes, {}, {remotes[0].nodes[0]: None}))
    assert not any("last_collection" in name for name in values)
    assert values['homelab_pdm_remote_collection_error{remote="lab",type="pve",reason=""}'] == "0"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (None, ""),
        ("", ""),
        ("bad fingerprint for ace", "fingerprint"),
        ("error:0A000086:SSL routines:certificate verify failed", "fingerprint"),
        ("api error (status = 401: authentication failure)", "auth"),
        ("api error (status = 403: permission check failed)", "auth"),
        ("error trying to connect: tcp connect error: Connection refused", "connect"),
        ("operation timed out", "connect"),
        ("dns error: failed to lookup address", "connect"),
        ("unexpected response shape", "other"),
    ],
)
def test_error_text_is_reduced_to_a_fixed_reason(exporter, error, reason):
    assert exporter.classify(error) == reason


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment\n",
        "pdm: self\n\tnodes a.example.internal\n",
        "\tnodes orphan.example.internal\n",
        "pve: empty\n\tauthid root@pam!pdm\n",
        "pve: bad\n\tnodes a.example.internal:notaport\n",
    ],
)
def test_unparseable_remotes_cfg_is_refused(exporter, text):
    with pytest.raises(exporter.ParseError):
        exporter.parse_remotes(text)


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"remotes": {}}', '{"remote-status": []}', '{"remote-status": {"x": 1}}'],
)
def test_unparseable_state_file_is_refused(exporter, text):
    with pytest.raises(exporter.ParseError):
        exporter.parse_state(text)


def _point_at(exporter, monkeypatch, tmp_path, remotes_cfg: str, state: str) -> Path:
    (tmp_path / "remotes.cfg").write_text(remotes_cfg, encoding="utf-8")
    (tmp_path / "state.json").write_text(state, encoding="utf-8")
    out_file = tmp_path / "pdm-remotes.prom"
    monkeypatch.setattr(exporter, "REMOTES_CFG", tmp_path / "remotes.cfg")
    monkeypatch.setattr(exporter, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", out_file)
    return out_file


def test_parse_failure_exits_nonzero_and_keeps_the_previous_file(exporter, tmp_path, monkeypatch):
    out_file = _point_at(exporter, monkeypatch, tmp_path, REMOTES_CFG, "{")
    out_file.write_text("homelab_pdm_remotes_total 3\n", encoding="utf-8")
    assert exporter.main() == 1
    assert out_file.read_text(encoding="utf-8") == "homelab_pdm_remotes_total 3\n"


def test_main_probes_every_node_and_writes_a_readable_file(exporter, tmp_path, monkeypatch):
    out_file = _point_at(exporter, monkeypatch, tmp_path, REMOTES_CFG, json.dumps(STATE))
    probed = []
    monkeypatch.setattr(exporter, "probe", lambda node: probed.append(node.short) or None)
    assert exporter.main() == 0
    assert sorted(probed) == ["ace", "bray", "osiris", "xur"]
    assert "homelab_pdm_remotes_total 3" in out_file.read_text(encoding="utf-8")
    assert out_file.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".pdm-remotes.prom.*"))


@pytest.fixture
def tls_server(tmp_path):
    """A one-shot TLS listener on localhost with a fresh self-signed certificate."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        try:
            with context.wrap_socket(conn, server_side=True):
                pass
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="utf-8"))
    yield port, hashlib.sha256(der).hexdigest()
    listener.close()
    thread.join(timeout=5)


def test_probe_reads_the_self_signed_certificate_actually_served(exporter, tls_server):
    port, expected = tls_server
    node = exporter.Node("127.0.0.1", port, expected)
    assert exporter.probe(node) == expected


def test_probe_of_a_closed_port_is_unreachable(exporter):
    with socket.create_server(("127.0.0.1", 0)) as placeholder:
        port = placeholder.getsockname()[1]
    assert exporter.probe(exporter.Node("127.0.0.1", port, None)) is None
