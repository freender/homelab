#!/usr/bin/env python3
"""Write each PBS remote's effective privileges for the node_exporter textfile collector.

xur's push account must stay add-only on the offsite PBS servers: Backup and
Read, never Prune or Modify, so a compromised xur cannot delete DR history.
Nothing on xur can see a grant made on the far side, and the pbs-server drift
audit there needs a manually loaded key. This checks it from xur every hour
with the credentials xur already holds: log in to each remote in
/etc/proxmox-backup/remote.cfg as its own auth-id and ask the remote for that
account's effective privileges (`GET /access/permissions`, which any account
may call for itself). Any privilege outside `ALLOWED` is published.

The remote's certificate fingerprint is checked on the same TLS connection,
before the password or token is written to it. A remote with no pinned
fingerprint is refused, never trusted on first use.

Label notes, as in pbs-backup-textfile-exporter: no `host` or `job` label,
both owned by the scrape config.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import os
import ssl
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

OUT_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
OUT_FILE = OUT_DIR / "pbs-remotes.prom"
REMOTE_CFG = Path(os.environ.get("PBS_REMOTE_CFG", "/etc/proxmox-backup/remote.cfg"))

# Indirection point for tests.
_connection = http.client.HTTPSConnection

TIMEOUT = 20
DEFAULT_PORT = 8007

# DatastoreBackup + DatastoreReader (Reader carries Verify on PBS 4). Prune,
# Modify, Allocate and every Sys./Permissions./Remote. privilege are excess.
ALLOWED = frozenset({"Datastore.Audit", "Datastore.Read", "Datastore.Backup", "Datastore.Verify"})

HEADERS = (
    "# HELP homelab_pbs_remotes_total Remotes configured on this PBS.",
    "# TYPE homelab_pbs_remotes_total gauge",
    "# HELP homelab_pbs_remote_check_ok 1 if the remote's privileges were read, 0 if "
    "the login, fingerprint or request failed.",
    "# TYPE homelab_pbs_remote_check_ok gauge",
    "# HELP homelab_pbs_remote_excess_privileges Privileges the remote grants this "
    "auth-id beyond Backup/Read/Audit/Verify; absent when the check failed.",
    "# TYPE homelab_pbs_remote_excess_privileges gauge",
    "# HELP homelab_pbs_remote_excess_privilege One series per excess privilege and path.",
    "# TYPE homelab_pbs_remote_excess_privilege gauge",
)


class RemoteError(Exception):
    pass


@dataclass(frozen=True)
class Remote:
    name: str
    host: str
    port: int
    auth_id: str
    fingerprint: str
    password: str


def parse_remotes(text: str) -> list[Remote]:
    """remote.cfg is PBS section config: `remote: <name>` then indented `key value`."""
    sections: list[tuple[str, dict[str, str]]] = []
    for line in text.splitlines():
        if line.startswith("remote:"):
            sections.append((line.split(":", 1)[1].strip(), {}))
        elif line[:1] in (" ", "\t") and line.strip() and sections:
            key, _, value = line.strip().partition(" ")
            sections[-1][1][key] = value.strip()
    return [
        Remote(
            name=name,
            host=props.get("host", ""),
            port=int(props.get("port") or DEFAULT_PORT),
            auth_id=props.get("auth-id", ""),
            fingerprint=props.get("fingerprint", "").replace(":", "").lower(),
            password=props.get("password", ""),
        )
        for name, props in sections
    ]


def secret(remote: Remote) -> str:
    """remote.cfg stores the password or token secret base64-encoded."""
    try:
        return base64.b64decode(remote.password, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise RemoteError("stored password is not base64") from exc


def request(
    remote: Remote, method: str, path: str, body: bytes | None, headers: dict[str, str]
) -> object:
    """One request on a fresh connection whose certificate is pinned before sending."""
    if not remote.fingerprint:
        raise RemoteError("no fingerprint pinned")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    conn = _connection(remote.host, remote.port, timeout=TIMEOUT, context=context)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        if not der or hashlib.sha256(der).hexdigest() != remote.fingerprint:
            raise RemoteError("certificate fingerprint mismatch")
        conn.request(method, f"/api2/json{path}", body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        if response.status != 200:
            raise RemoteError(f"{method} {path} returned HTTP {response.status}")
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise RemoteError(f"{method} {path}: {exc}") from exc
    finally:
        conn.close()
    try:
        return json.loads(payload)["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"{method} {path}: unexpected response") from exc


def auth_headers(remote: Remote) -> dict[str, str]:
    if "!" in remote.auth_id:
        return {"Authorization": f"PBSAPIToken={remote.auth_id}:{secret(remote)}"}
    form = urllib.parse.urlencode({"username": remote.auth_id, "password": secret(remote)})
    ticket = request(
        remote,
        "POST",
        "/access/ticket",
        form.encode(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not isinstance(ticket, dict) or "ticket" not in ticket:
        raise RemoteError("login returned no ticket")
    return {"Cookie": f"PBSAuthCookie={urllib.parse.quote(ticket['ticket'])}"}


def excess_privileges(permissions: object) -> list[tuple[str, str]]:
    if not isinstance(permissions, dict):
        raise RemoteError("permissions response is not a mapping")
    return sorted(
        (path, privilege)
        for path, privileges in permissions.items()
        for privilege in privileges or {}
        if privilege not in ALLOWED
    )


def check(remote: Remote) -> list[tuple[str, str]]:
    permissions = request(remote, "GET", "/access/permissions", None, auth_headers(remote))
    return excess_privileges(permissions)


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def labels(**pairs: str) -> str:
    return ",".join(f'{key}="{escape(value)}"' for key, value in pairs.items())


def remote_lines(remote: Remote, result: list[tuple[str, str]] | None) -> list[str]:
    base = labels(remote=remote.name, auth_id=remote.auth_id)
    lines = [f"homelab_pbs_remote_check_ok{{{base}}} {0 if result is None else 1}"]
    if result is None:
        return lines
    lines.append(f"homelab_pbs_remote_excess_privileges{{{base}}} {len(result)}")
    for path, privilege in result:
        extra = labels(remote=remote.name, auth_id=remote.auth_id, path=path, privilege=privilege)
        lines.append(f"homelab_pbs_remote_excess_privilege{{{extra}}} 1")
    return lines


def render(results: list[tuple[Remote, list[tuple[str, str]] | None]]) -> str:
    lines = [*HEADERS, f"homelab_pbs_remotes_total {len(results)}"]
    for remote, result in results:
        lines.extend(remote_lines(remote, result))
    return "\n".join(lines) + "\n"


def write(content: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, prefix=".pbs-remotes.prom.")
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
        remotes = parse_remotes(REMOTE_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Keep the previous file: the stale-file rule catches one that stops
        # being rewritten, while an empty one would read as "no remotes".
        print(f"pbs-remote-textfile-exporter: {exc}", file=sys.stderr)
        return 1

    results: list[tuple[Remote, list[tuple[str, str]] | None]] = []
    for remote in remotes:
        try:
            results.append((remote, check(remote)))
        except RemoteError as exc:
            print(f"pbs-remote-textfile-exporter: {remote.name}: {exc}", file=sys.stderr)
            results.append((remote, None))
    write(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
