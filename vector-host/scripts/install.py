#!/usr/bin/env python3
"""Remote installer for the vector-host module (freender/homelab-ops#377).

Host-native Vector shipping the systemd journal to VictoriaLogs, for the hosts
with no Docker Vector. Worth knowing before reading:

* **Vector comes from Datadog's apt repo, unpinned** (homelab-ops#376), with the
  keyring vendored and `--no-install-recommends`, so `datadog-signing-keys` never
  installs a second copy of it. A fresh install neither enables nor starts the
  unit; this does, after the config is in place.
* **The staged config is checked with the installed binary** before anything is
  written: `vector validate --no-environment`, then `vector test`. The wrapper
  goes in first, because the config names it as `journalctl_path`.
* **The unit drop-in** retries every 30 s with no start limit and validates
  without healthchecks, and points `VECTOR_CONFIG` at the module's own file.
* **Delivery is confirmed end to end.** After a (re)start the installer logs a
  marker carrying a nonce and waits for VictoriaLogs to return it from the
  tenant the config writes to. An active unit alone proves nothing: Vector keeps
  running with its sink retrying, and the journald checkpoint advances as soon as
  a batch reaches the disk buffer, not VictoriaLogs.
"""

from __future__ import annotations

import grp
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

PACKAGE = "vector"
UNIT = "vector.service"
VECTOR = "/usr/bin/vector"
JOURNAL_GROUP = "systemd-journal"
KEYRING_NAME = "datadog.asc"
SOURCE_NAME = "vector.list"
WRAPPER_NAME = "journalctl-catchup"
OVERRIDE_NAME = "vector-override.conf"
CONFIG_NAME = "vector.yaml"
TESTS_NAME = "vector.test.yaml"
STAGED = (KEYRING_NAME, SOURCE_NAME, WRAPPER_NAME, OVERRIDE_NAME, CONFIG_NAME, TESTS_NAME)
MARKER_TAG = "vector-host"
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


def preflight(ctx: InstallContext) -> None:
    """Every refusal, before any write."""
    env.require(ctx, "VECTOR_QUERY_URL", "VECTOR_TENANT")
    for name in STAGED:
        if not (ctx.build_dir / name).is_file():
            raise InstallError(f"missing staged file: {ctx.build_dir / name}")
    for name in STAGED[:-1]:
        if name not in ctx.file_map:
            raise InstallError(f"missing file-map entry: {name}")


def ensure_package(ctx: InstallContext) -> None:
    sources = [files.install(ctx, KEYRING_NAME), files.install(ctx, SOURCE_NAME)]
    if any(sources):
        packages.sources_changed(ctx)
    packages.ensure(ctx, PACKAGE, no_recommends=True)


def require_journal_access() -> None:
    """The package's postinst adds `vector` to systemd-journal; without it the
    source reads only Vector's own lines and nothing fails."""
    try:
        members = grp.getgrnam(JOURNAL_GROUP).gr_mem
    except KeyError as exc:
        raise InstallError(f"group {JOURNAL_GROUP} does not exist") from exc
    if PACKAGE not in members:
        raise InstallError(f"user {PACKAGE} is not in {JOURNAL_GROUP}; it cannot read the journal")


def _vector(*args: str) -> subprocess.CompletedProcess:
    return _run([VECTOR, *args], check=False, capture_output=True, text=True)


def check_config(ctx: InstallContext) -> None:
    config = str(ctx.build_dir / CONFIG_NAME)
    log.sub("Validating staged Vector config...")
    result = _vector("validate", "--no-environment", config)
    if result.returncode != 0:
        log.sub((result.stdout or result.stderr or "").strip() or "<no output>")
        raise InstallError(f"vector rejected the staged config (exit {result.returncode})")

    log.sub("Running Vector unit tests...")
    result = _vector("test", config, str(ctx.build_dir / TESTS_NAME))
    if result.returncode != 0:
        log.sub((result.stdout or result.stderr or "").strip() or "<no output>")
        raise InstallError(f"vector unit tests failed (exit {result.returncode})")


def is_active() -> bool:
    return _run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0


def found_in_victoria_logs(url: str, tenant: str, marker: str) -> bool:
    query = urllib.parse.urlencode({"query": f'_time:5m "{marker}"', "limit": "1"})
    request = urllib.request.Request(f"{url}?{query}", headers={"AccountID": tenant})
    try:
        with _urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as response:
            return bool(response.read().strip())
    except OSError as exc:
        log.sub(f"VictoriaLogs query failed: {exc}")
        return False


def confirm_delivery(url: str, tenant: str) -> None:
    """Log a marker until VictoriaLogs returns it. Repeated, because the first may
    land before the journald source has started reading."""
    marker = f"delivery check {_nonce()}"
    log.sub(f"Waiting for VictoriaLogs tenant {tenant} to receive a journal marker...")
    deadline = _now() + DELIVERY_TIMEOUT_SECONDS
    while True:
        _run(["logger", "-t", MARKER_TAG, marker], check=False)
        _sleep(DELIVERY_POLL_SECONDS)
        if found_in_victoria_logs(url, tenant, marker):
            log.ok("VictoriaLogs received journal entries")
            return
        if _now() >= deadline:
            raise InstallError(
                f"{UNIT} delivered nothing within {DELIVERY_TIMEOUT_SECONDS:.0f}s; "
                f"check `journalctl -u {UNIT}`"
            )


def install(ctx: InstallContext) -> None:
    log.header("Vector Host")
    preflight(ctx)

    log.action("Vector package")
    ensure_package(ctx)
    require_journal_access()

    log.action("Vector config")
    # Before the check: the config names it as journalctl_path.
    changed = files.install(ctx, WRAPPER_NAME)
    check_config(ctx)
    # Each its own call, never short-circuited.
    changed = files.install(ctx, OVERRIDE_NAME) or changed
    changed = files.install(ctx, CONFIG_NAME) or changed

    starting = changed or not is_active()
    # ensure_running daemon-reloads before the restart when anything changed.
    systemd.ensure_running(ctx, UNIT, changed)
    systemd.require_active(ctx, UNIT)
    if starting:
        confirm_delivery(ctx.env["VECTOR_QUERY_URL"], ctx.env["VECTOR_TENANT"])


if __name__ == "__main__":
    run(install, "Vector Host")
