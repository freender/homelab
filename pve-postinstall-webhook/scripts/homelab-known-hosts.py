#!/usr/bin/env python3
"""Keep PVE node host keys in /etc/ssh/ssh_known_hosts on arc and the push targets.

A rebuilt node gets new host keys, and every client that still holds the old ones
refuses to connect. arc keeps one file of trusted keys per node in STORE, renders
them into its own system known_hosts under each node's short name, FQDN and mgmt
IP (NAMES_CONF, generated from hosts.conf), and copies the same file to every
`KNOWN_HOSTS_PUSH` target (riven). A correct key of the negotiated type in the
system file wins over a stale one in a user's own known_hosts, so nobody needs
`ssh-keygen -R` (freender/homelab-ops#279, #281).

Keys only change on a rebuild, never on a timer:

  record <node> <event>  the installer's own keys from PDM's post-hook payload,
                         sent with the per-install token before arc connects
  capture <node>         fallback for a by-hand deploy with no event file: the keys
                         the node presents to the worker's accept-new SSH
  seed                   every node with no stored keys, read over SSH that must
                         already trust the node (StrictHostKeyChecking=yes)
  sync                   re-render and re-push only

Each command re-renders and re-pushes. Exit 0 on success, 1 when keys were stored
but a render or push failed, 3 when `record` found no keys to store.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

STORE = Path("/var/lib/homelab-postinstall-webhook/host-keys")
NAMES_CONF = Path("/etc/homelab-postinstall-webhook/pve-known-hosts.conf")
CONFIG_FILE = Path("/etc/homelab-postinstall-webhook/poller.env")
KNOWN_HOSTS = Path("/etc/ssh/ssh_known_hosts")
HEADER = "# Managed by homelab pve-postinstall-webhook (homelab-known-hosts); do not edit.\n"
NO_KEYS = 3

SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
# The post-install worker's own options: trust on first use after its `-R`.
SSH_ACCEPT_NEW = ["-o", "StrictHostKeyChecking=accept-new"]
SSH_STRICT = ["-o", "StrictHostKeyChecking=yes"]
USER_KNOWN_HOSTS = ["-o", "UserKnownHostsFile=/root/.ssh/known_hosts"]
READ_HOST_KEYS = "cat /etc/ssh/ssh_host_*_key.pub"
REMOTE_KNOWN_HOSTS = "/etc/ssh/ssh_known_hosts"
PUSH_SCRIPT = f"""set -eu
dest={REMOTE_KNOWN_HOSTS}
tmp=/etc/ssh/.ssh_known_hosts.homelab.tmp
cat >"$tmp"
if cmp -s "$tmp" "$dest"; then rm -f "$tmp"; echo unchanged; exit 0; fi
chmod 644 "$tmp"
mv -f "$tmp" "$dest"
echo updated
"""


def log(message: str) -> None:
    print(f"homelab-known-hosts: {message}", flush=True)


def warn(message: str) -> None:
    print(f"homelab-known-hosts: WARNING: {message}", file=sys.stderr, flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed = shlex.split(value, posix=True)
        result[key.strip()] = parsed[0] if parsed else ""
    return result


def push_targets() -> list[str]:
    env = {**load_env_file(CONFIG_FILE), **os.environ}
    return env.get("KNOWN_HOSTS_PUSH", "").split()


def key_line(text: str) -> str | None:
    """`<type> <base64>` from a public key line, dropping the comment."""
    fields = text.split()
    if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-")):
        return None
    return f"{fields[0]} {fields[1]}"


def normalize_keys(lines: list[str]) -> str:
    keys = sorted({line for line in (key_line(raw) for raw in lines) if line})
    return "".join(f"{key}\n" for key in keys)


def payload_keys(event: dict) -> str:
    """The installer-reported keys: `ssh-public-host-keys` is `{type: line}`."""
    keys = (event.get("post-hook-data") or {}).get("ssh-public-host-keys") or {}
    if not isinstance(keys, dict):
        return ""
    return normalize_keys([str(value) for value in keys.values()])


def write_if_changed(path: Path, content: str, mode: int) -> bool:
    """Write via a temp file and rename, only when the content differs."""
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)
    return True


def store_keys(node: str, keys: str, source: str) -> None:
    STORE.mkdir(mode=0o755, parents=True, exist_ok=True)
    if write_if_changed(STORE / node, keys, 0o644):
        log(f"stored {len(keys.splitlines())} host keys for {node} ({source})")
    else:
        log(f"host keys for {node} unchanged ({source})")


def read_names(conf: Path) -> dict[str, list[str]]:
    """`<node> <fqdn> <mgmt ip>` per line, the node's short name first."""
    names: dict[str, list[str]] = {}
    for raw in conf.read_text(encoding="utf-8").splitlines():
        fields = raw.split("#", 1)[0].split()
        if fields:
            names[fields[0]] = list(dict.fromkeys(fields))
    return names


def ssh_target(names: dict[str, list[str]], node: str) -> str:
    """root at the node's FQDN, the second name; the short name if there is none."""
    host_names = names.get(node, [node])
    return f"root@{host_names[1] if len(host_names) > 1 else host_names[0]}"


def run_ssh(options: list[str], target: str, command: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        [*SSH_BASE, *options, *USER_KNOWN_HOSTS, target, command],
        input=stdin,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def remote_host_keys(target: str, options: list[str]) -> str:
    keys = normalize_keys(run_ssh(options, target, READ_HOST_KEYS).splitlines())
    if not keys:
        raise RuntimeError(f"{target} reported no SSH host public keys")
    return keys


def render(names: dict[str, list[str]]) -> tuple[str, list[str]]:
    """The known_hosts text and the nodes it covers, in inventory order.

    Only inventory nodes are rendered, so a retired node's stored keys drop out
    with its hosts.conf entry.
    """
    lines: list[str] = []
    covered: list[str] = []
    for node, host_names in names.items():
        stored = STORE / node
        if not stored.is_file():
            continue
        keys = normalize_keys(stored.read_text(encoding="utf-8").splitlines()).splitlines()
        if not keys:
            continue
        lines.extend(f"{','.join(host_names)} {key}\n" for key in keys)
        covered.append(node)
    return HEADER + "".join(lines), covered


def push(content: str, target: str) -> bool:
    try:
        outcome = run_ssh(SSH_ACCEPT_NEW, target, PUSH_SCRIPT, stdin=content).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"push to {target} failed: {describe(exc)}")
        return False
    log(f"{target}:{REMOTE_KNOWN_HOSTS} {outcome}")
    return True


def describe(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    return stderr.strip() if isinstance(stderr, str) and stderr.strip() else str(exc)


def sync(names: dict[str, list[str]]) -> int:
    content, covered = render(names)
    if not covered:
        warn("no stored host keys; leaving known_hosts alone")
        return 1
    if write_if_changed(KNOWN_HOSTS, content, 0o644):
        log(f"rendered {KNOWN_HOSTS} for {' '.join(covered)}")
    results = [push(content, target) for target in push_targets()]
    return 0 if all(results) else 1


def cmd_record(names: dict[str, list[str]], node: str, event_path: Path) -> int:
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"cannot read event {event_path}: {exc}")
        return NO_KEYS
    keys = payload_keys(event) if isinstance(event, dict) else ""
    if not keys:
        warn(f"event {event_path} carries no SSH host keys for {node}")
        return NO_KEYS
    store_keys(node, keys, f"post-hook payload {event_path.name}")
    return sync(names)


def cmd_capture(names: dict[str, list[str]], node: str) -> int:
    target = ssh_target(names, node)
    try:
        keys = remote_host_keys(target, SSH_ACCEPT_NEW)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        warn(f"cannot read host keys from {target}: {describe(exc)}")
        return 1
    store_keys(node, keys, f"first use over SSH to {target}")
    return sync(names)


def cmd_seed(names: dict[str, list[str]]) -> int:
    for node in names:
        if (STORE / node).is_file():
            continue
        target = ssh_target(names, node)
        try:
            keys = remote_host_keys(target, SSH_STRICT)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            warn(f"cannot seed {node} from already-trusted {target}: {describe(exc)}")
            continue
        store_keys(node, keys, f"seeded over trusted SSH to {target}")
    return sync(names)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="homelab-known-hosts")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("node")
    record.add_argument("event", type=Path)
    capture = commands.add_parser("capture")
    capture.add_argument("node")
    commands.add_parser("seed")
    commands.add_parser("sync")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    names = read_names(NAMES_CONF)
    if args.command == "record":
        return cmd_record(names, args.node, args.event)
    if args.command == "capture":
        return cmd_capture(names, args.node)
    if args.command == "seed":
        return cmd_seed(names)
    return sync(names)


if __name__ == "__main__":
    sys.exit(main())
