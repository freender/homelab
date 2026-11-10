#!/usr/bin/env python3
"""Remote installer for the journal-upload module (freender/homelab-ops#344).

Ships the host's systemd journal to VictoriaLogs with Debian's
`systemd-journal-upload`. Two details are specific to it:

* **The first start is seeded at the journal's current end.** With no state
  file the uploader sends the whole journal, and a full backlog overruns
  VictoriaLogs' `-journald.maxRequestSize` (VictoriaLogs#142). The installer
  writes the state file the uploader itself keeps, holding the cursor of the
  newest entry, before the unit ever starts. It is written under
  `/var/lib/private` because the unit runs with `DynamicUser=yes`; systemd
  re-owns the directory to the dynamic user on start.
* **Delivery is confirmed from that same cursor.** The uploader rewrites the
  state file only after VictoriaLogs accepts a batch, so a cursor that moves
  past its pre-start value proves the endpoint took data. An active unit alone
  proves nothing: a bad URL fails the upload and the unit keeps restarting.

It also ships a service drop-in that retries every 30 s with no start limit.
Debian's unit restarts after 100 ms under systemd's default 5-in-10 s limit, so
a VictoriaLogs outage of more than a few seconds left the unit failed for good
(freender/homelab-ops#373).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

PACKAGE = "systemd-journal-remote"
UNIT = "systemd-journal-upload.service"
CONFIG_NAME = "journal-upload.conf"
OVERRIDE_NAME = "service-override.conf"
PRIVATE_ROOT = Path("/var/lib/private")
STATE_FILE = PRIVATE_ROOT / "systemd" / "journal-upload" / "state"
STATE_HEADER = "# This is private data. Do not parse.\n"
CURSOR_KEY = "LAST_CURSOR="
DELIVERY_TIMEOUT_SECONDS = 30.0
DELIVERY_POLL_SECONDS = 1.0

# Indirection points for tests, matching packages.py / systemd.py.
_run = subprocess.run
_sleep = time.sleep
_now = time.monotonic


def read_cursor() -> str | None:
    if not STATE_FILE.is_file():
        return None
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(CURSOR_KEY):
            return line[len(CURSOR_KEY):]
    return None


def journal_head_cursor() -> str:
    result = _run(
        ["journalctl", "--lines=1", "--output=json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"journalctl failed (exit {result.returncode})")
    try:
        cursor = json.loads(result.stdout)["__CURSOR"]
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError("could not read the journal's newest cursor") from exc
    return cursor


def seed_cursor() -> bool:
    """Start the uploader at the journal's current end, once. Returns True if seeded."""
    if STATE_FILE.is_file():
        log.sub("Upload state exists; resuming from its cursor")
        return False

    cursor = journal_head_cursor()
    PRIVATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    PRIVATE_ROOT.chmod(0o700)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(f"{STATE_HEADER}{CURSOR_KEY}{cursor}\n", encoding="utf-8")
    log.ok("Seeded upload state at the journal's newest entry")
    return True


def is_active() -> bool:
    return _run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0


def confirm_delivery(before: str | None) -> None:
    """Wait for the uploader to move its cursor past `before`."""
    log.sub("Waiting for VictoriaLogs to accept an upload...")
    deadline = _now() + DELIVERY_TIMEOUT_SECONDS
    while True:
        cursor = read_cursor()
        if cursor is not None and cursor != before:
            log.ok("VictoriaLogs accepted journal entries")
            return
        if _now() >= deadline:
            raise InstallError(
                f"{UNIT} delivered nothing within {DELIVERY_TIMEOUT_SECONDS:.0f}s; "
                f"check `journalctl -u {UNIT}`"
            )
        _sleep(DELIVERY_POLL_SECONDS)


def install(ctx: InstallContext) -> None:
    log.header("Journal Upload")

    env.require(ctx, "JOURNAL_UPLOAD_DEST", "JOURNAL_UPLOAD_OVERRIDE_DEST")
    src = ctx.build_dir / CONFIG_NAME
    override = ctx.build_dir / OVERRIDE_NAME
    for staged in (src, override):
        if not staged.is_file():
            raise InstallError(f"missing staged journal-upload config: {staged}")

    packages.ensure(ctx, PACKAGE)
    config_changed = files.install_from(ctx, src, ctx.env["JOURNAL_UPLOAD_DEST"], "0644")
    override_changed = files.install_from(
        ctx, override, ctx.env["JOURNAL_UPLOAD_OVERRIDE_DEST"], "0644"
    )
    # ensure_running daemon-reloads before the restart when either changed.
    changed = config_changed or override_changed
    seeded = seed_cursor()
    starting = changed or seeded or not is_active()

    before = read_cursor()
    # A running uploader holds its cursor in memory; only a restart reads a seed.
    systemd.ensure_running(ctx, UNIT, changed or seeded)
    systemd.require_active(ctx, UNIT)
    if starting:
        # The (re)start itself writes journal entries, so there is always
        # something new to upload.
        confirm_delivery(before)


if __name__ == "__main__":
    run(install, "Journal Upload")
