#!/usr/bin/env python3
"""Write PDM remote poll state for the node_exporter textfile collector.

Proxmox Datacenter Manager has no notification subsystem, so a remote it can no
longer poll (a bad fingerprint pin, a revoked token, an unreachable node) only
ever showed up in its own log. Two local sources, no API token:

  * /var/lib/proxmox-datacenter-manager/metric-collection-state.json -- PDM's own
    per-remote result of every metric collection run: `last-collection` moves on
    success only, `error` holds the last failure.
  * /etc/proxmox-datacenter-manager/remotes.cfg -- every pinned node. Each one is
    probed over TLS on its API port and the served certificate's SHA-256 is
    compared with the pin, the same check PDM makes.

The per-node probe is the part remote-level status cannot replace: a cluster
remote falls back to its other nodes, so one node with a stale pin never fails
the remote's collection.

Label notes:

  * No `host` label -- the scrape config sets `host="arc"`. The pinned node's
    short name is `node_host`; the rules rename it to `host` so NodeDown
    inhibition applies.
  * `reason` is a fixed class (fingerprint/auth/connect/other), never the raw
    error text.

A file that cannot be parsed exits non-zero and leaves the previous .prom alone:
PDM's state file is internal, not API, so format drift has to surface as a
failed unit and a missing/stale series rather than as "no errors".
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

OUT_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
OUT_FILE = OUT_DIR / "pdm-remotes.prom"

STATE_FILE = Path(
    os.environ.get(
        "PDM_STATE_FILE", "/var/lib/proxmox-datacenter-manager/metric-collection-state.json"
    )
)
REMOTES_CFG = Path(
    os.environ.get("PDM_REMOTES_CFG", "/etc/proxmox-datacenter-manager/remotes.cfg")
)

DEFAULT_PORTS = {"pve": 8006, "pbs": 8007}
PROBE_TIMEOUT = 5

# First match wins; checked against the lowercased error text.
REASONS = (
    ("fingerprint", ("fingerprint", "certificate verify")),
    ("auth", ("401", "403", "authentication", "permission")),
    ("connect", ("connect", "timed out", "dns")),
)

HEADERS = (
    "# HELP homelab_pdm_remotes_total Remotes configured in PDM.",
    "# TYPE homelab_pdm_remotes_total gauge",
    "# HELP homelab_pdm_remote_last_collection_timestamp_seconds Unix time of PDM's "
    "last successful metric collection from the remote.",
    "# TYPE homelab_pdm_remote_last_collection_timestamp_seconds gauge",
    "# HELP homelab_pdm_remote_collection_error 1 if PDM's last metric collection "
    "from the remote failed, with the failure class in `reason`.",
    "# TYPE homelab_pdm_remote_collection_error gauge",
    "# HELP homelab_pdm_remote_node_reachable 1 if a TLS handshake with the pinned "
    "node's API port succeeded.",
    "# TYPE homelab_pdm_remote_node_reachable gauge",
    "# HELP homelab_pdm_remote_node_fingerprint_match 1 if the certificate the node "
    "serves matches PDM's pin; absent while the node is unreachable.",
    "# TYPE homelab_pdm_remote_node_fingerprint_match gauge",
)


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class Node:
    address: str
    port: int
    fingerprint: str | None

    @property
    def short(self) -> str:
        """`ace` for `ace.example.internal`; an IP is kept whole."""
        if self.address.replace(".", "").isdigit() or ":" in self.address:
            return self.address
        return self.address.split(".", 1)[0]


@dataclass
class Remote:
    name: str
    kind: str
    nodes: list[Node] = field(default_factory=list)


def normalize_fingerprint(value: str) -> str:
    return value.replace(":", "").strip().lower()


def parse_node(value: str, kind: str) -> Node:
    """`<host>[:port][,fingerprint=AA:BB:..]` from a `nodes` line."""
    address, _, options = value.partition(",")
    fingerprint = None
    for option in filter(None, (part.strip() for part in options.split(","))):
        key, _, setting = option.partition("=")
        if key == "fingerprint":
            fingerprint = normalize_fingerprint(setting)
    port = DEFAULT_PORTS[kind]
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        if rest.startswith(":"):
            port = int(rest[1:])
        address = host
    elif address.count(":") == 1:
        address, _, port_text = address.partition(":")
        port = int(port_text)
    if not address:
        raise ParseError(f"empty node address in {value!r}")
    return Node(address, port, fingerprint)


def parse_remotes(text: str) -> list[Remote]:
    """Section-config: `<type>: <id>` headers, indented `<key> <value>` lines."""
    remotes: list[Remote] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            kind, sep, name = raw.partition(":")
            kind, name = kind.strip(), name.strip()
            if not sep or not name or kind not in DEFAULT_PORTS:
                raise ParseError(f"remotes.cfg line {number}: unexpected section {raw!r}")
            remotes.append(Remote(name, kind))
            continue
        if not remotes:
            raise ParseError(f"remotes.cfg line {number}: property before any section")
        key, _, value = raw.strip().partition(" ")
        if key == "nodes":
            try:
                remotes[-1].nodes.append(parse_node(value.strip(), remotes[-1].kind))
            except ValueError as exc:
                raise ParseError(f"remotes.cfg line {number}: {exc}") from exc
    if not remotes:
        raise ParseError("remotes.cfg defines no remotes")
    for remote in remotes:
        if not remote.nodes:
            raise ParseError(f"remote {remote.name} has no nodes")
    return remotes


def parse_state(text: str) -> dict[str, dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"state file is not JSON: {exc}") from exc
    status = data.get("remote-status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        raise ParseError("state file has no remote-status object")
    for name, entry in status.items():
        if not isinstance(entry, dict):
            raise ParseError(f"remote-status.{name} is not an object")
    return status


def classify(error: str | None) -> str:
    if not error:
        return ""
    text = error.lower()
    for reason, needles in REASONS:
        if any(needle in text for needle in needles):
            return reason
    return "other"


def probe(node: Node) -> str | None:
    """SHA-256 of the certificate the node serves, or None when unreachable.

    Verification is off on purpose: the point is to read whatever is served and
    compare it with the pin, including a self-signed or mismatched certificate.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((node.address, node.port), timeout=PROBE_TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=node.address) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    return hashlib.sha256(der).hexdigest() if der else None


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def labels(**pairs: str) -> str:
    return ",".join(f'{key}="{escape(value)}"' for key, value in pairs.items())


def remote_lines(remote: Remote, state: dict) -> list[str]:
    block = labels(remote=remote.name, type=remote.kind)
    lines = []
    last = state.get("last-collection")
    if isinstance(last, int | float) and last > 0:
        lines.append(
            f"homelab_pdm_remote_last_collection_timestamp_seconds{{{block}}} {int(last)}"
        )
    reason = classify(state.get("error"))
    error_block = labels(remote=remote.name, type=remote.kind, reason=reason)
    lines.append(f"homelab_pdm_remote_collection_error{{{error_block}}} {1 if reason else 0}")
    return lines


def node_lines(remote: Remote, node: Node, served: str | None) -> list[str]:
    block = labels(remote=remote.name, node=node.address, node_host=node.short)
    lines = [f"homelab_pdm_remote_node_reachable{{{block}}} {0 if served is None else 1}"]
    if served is not None and node.fingerprint:
        match = 1 if served == node.fingerprint else 0
        lines.append(f"homelab_pdm_remote_node_fingerprint_match{{{block}}} {match}")
    return lines


def render(remotes: list[Remote], status: dict[str, dict], probes: dict[Node, str | None]) -> str:
    lines = [*HEADERS, f"homelab_pdm_remotes_total {len(remotes)}"]
    for remote in sorted(remotes, key=lambda item: item.name):
        lines.extend(remote_lines(remote, status.get(remote.name, {})))
        for node in remote.nodes:
            lines.extend(node_lines(remote, node, probes[node]))
    return "\n".join(lines) + "\n"


def write(content: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, prefix=".pdm-remotes.prom.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, OUT_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise


def main() -> int:
    try:
        remotes = parse_remotes(REMOTES_CFG.read_text(encoding="utf-8"))
        status = parse_state(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ParseError) as exc:
        print(f"pdm-remote-textfile-exporter: {exc}", file=sys.stderr)
        return 1

    probes = {node: probe(node) for remote in remotes for node in remote.nodes}
    write(render(remotes, status, probes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
