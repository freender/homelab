"""Host-native Vector: the config check and end-to-end delivery check.

Shared by `vector-host` (apt hosts) and `vr-guest` (solar's pinned tarball,
freender/homelab-ops#402), which install Vector differently but must prove the
same two things before and after a (re)start:

* **The staged config passes the installed binary** before anything is written:
  `vector validate --no-environment`, then `vector test`.
* **Delivery is confirmed end to end.** A marker carrying a nonce is logged until
  VictoriaLogs returns it from the tenant the config writes to. An active unit
  alone proves nothing: Vector keeps running with its sink retrying, and the
  journald checkpoint advances as soon as a batch reaches the disk buffer, not
  VictoriaLogs.
"""

from __future__ import annotations

import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import log
from .errors import InstallError

DELIVERY_TIMEOUT_SECONDS = 30.0
DELIVERY_POLL_SECONDS = 2.0
QUERY_TIMEOUT_SECONDS = 5.0

# Indirection points for tests, matching packages.py / systemd.py.
_run = subprocess.run
_sleep = time.sleep
_now = time.monotonic
_urlopen = urllib.request.urlopen


def _nonce() -> str:
    return uuid.uuid4().hex


def _vector(binary: str, *args: str) -> subprocess.CompletedProcess:
    return _run([binary, *args], check=False, capture_output=True, text=True)


def check_config(binary: str, config: Path, tests: Path) -> None:
    log.sub("Validating staged Vector config...")
    result = _vector(binary, "validate", "--no-environment", str(config))
    if result.returncode != 0:
        log.sub((result.stdout or result.stderr or "").strip() or "<no output>")
        raise InstallError(f"vector rejected the staged config (exit {result.returncode})")

    log.sub("Running Vector unit tests...")
    result = _vector(binary, "test", str(config), str(tests))
    if result.returncode != 0:
        log.sub((result.stdout or result.stderr or "").strip() or "<no output>")
        raise InstallError(f"vector unit tests failed (exit {result.returncode})")


def found_in_victoria_logs(url: str, tenant: str, marker: str) -> bool:
    query = urllib.parse.urlencode({"query": f'_time:5m "{marker}"', "limit": "1"})
    request = urllib.request.Request(f"{url}?{query}", headers={"AccountID": tenant})
    try:
        with _urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as response:
            return bool(response.read().strip())
    except OSError as exc:
        log.sub(f"VictoriaLogs query failed: {exc}")
        return False


def confirm_delivery(url: str, tenant: str, tag: str, unit: str) -> None:
    """Log a marker under `tag` until VictoriaLogs returns it. Repeated, because
    the first may land before the journald source has started reading."""
    marker = f"delivery check {_nonce()}"
    log.sub(f"Waiting for VictoriaLogs tenant {tenant} to receive a journal marker...")
    deadline = _now() + DELIVERY_TIMEOUT_SECONDS
    while True:
        _run(["logger", "-t", tag, marker], check=False)
        _sleep(DELIVERY_POLL_SECONDS)
        if found_in_victoria_logs(url, tenant, marker):
            log.ok("VictoriaLogs received journal entries")
            return
        if _now() >= deadline:
            raise InstallError(
                f"{unit} delivered nothing within {DELIVERY_TIMEOUT_SECONDS:.0f}s; "
                f"check `journalctl -u {unit}`"
            )
