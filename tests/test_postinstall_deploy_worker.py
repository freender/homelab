"""`homelab-postinstall-deploy`'s readiness SSH on arc (freender/homelab-ops#281).

The worker runs for real up to its deploy step; `ssh`, `ssh-keygen` and
`homelab-known-hosts` are stubs that log their arguments, and the deploy's lock
file is out of reach, so nothing past the readiness check and key capture runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKER = (
    Path(__file__).resolve().parents[1]
    / "pve-postinstall-webhook"
    / "scripts"
    / "homelab-postinstall-deploy.sh"
)

HOSTS_CONF = """\
ace:
  config:
    type: pve
    hostname: ace.example.internal
    user: root
"""


def stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def run_worker(tmp_path: Path, record_status: int, with_event: bool) -> dict[str, list[str]]:
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (repo / "hosts.conf").write_text(HOSTS_CONF, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    stub(bin_dir / "ssh", f'echo "ssh $*" >>{log}')
    stub(bin_dir / "ssh-keygen", f'echo "ssh-keygen $*" >>{log}')
    tool = tmp_path / "homelab-known-hosts"
    stub(tool, f'echo "tool $*" >>{log}\nexit {record_status}')
    event = tmp_path / "event.json"
    event.write_text(json.dumps({}), encoding="utf-8")

    args = ["bash", str(WORKER), "ace"] + ([str(event)] if with_event else [])
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REPO_DIR": str(repo),
        "DRY_RUN": "false",
        "SSH_TIMEOUT_SECONDS": "30",
        "KNOWN_HOSTS_TOOL": str(tool),
        "HOME": str(tmp_path),
    }
    subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, check=False)

    calls: dict[str, list[str]] = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        name, _, rest = line.partition(" ")
        calls.setdefault(name, []).append(rest)
    return calls


def test_a_recorded_payload_keeps_the_system_known_hosts_in_play(tmp_path) -> None:
    calls = run_worker(tmp_path, record_status=0, with_event=True)

    assert calls["tool"] == [f"record ace {tmp_path / 'event.json'}"]
    readiness = calls["ssh"][0]
    assert "StrictHostKeyChecking=accept-new" in readiness
    assert "GlobalKnownHostsFile" not in readiness


def test_a_by_hand_run_skips_the_stale_system_file_then_captures(tmp_path) -> None:
    """No event: the system file still holds the pre-rebuild key, so the first-use
    check must not read it, and capture records what that check accepted."""
    calls = run_worker(tmp_path, record_status=0, with_event=False)

    readiness = calls["ssh"][0]
    assert "StrictHostKeyChecking=accept-new" in readiness
    assert "GlobalKnownHostsFile=/dev/null" in readiness
    assert readiness.endswith("root@ace.example.internal systemctl is-active --quiet "
                              "pve-cluster && pvesh get /version >/dev/null")
    assert calls["tool"] == ["capture ace"]


def test_a_payload_without_keys_falls_back_to_first_use(tmp_path) -> None:
    calls = run_worker(tmp_path, record_status=3, with_event=True)

    assert "GlobalKnownHostsFile=/dev/null" in calls["ssh"][0]
    assert calls["tool"][-1] == "capture ace"
