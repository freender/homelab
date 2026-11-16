#!/usr/bin/env python3
"""Remote installer for the vector-host module (freender/homelab-ops#377).

Host-native Vector shipping the systemd journal to VictoriaLogs, for the hosts
with no Docker Vector. Worth knowing before reading:

* **Vector comes from Datadog's apt repo, unpinned** (homelab-ops#376), with the
  keyring vendored and `--no-install-recommends`, so `datadog-signing-keys` never
  installs a second copy of it. A fresh install neither enables nor starts the
  unit; this does, after the config is in place.
* **The staged config is checked with the installed binary** before anything is
  written (`homelab_install.vector`). The wrapper goes in first, because the
  config names it as `journalctl_path`.
* **The unit drop-in** retries every 30 s with no start limit and validates
  without healthchecks, and points `VECTOR_CONFIG` at the module's own file.
* **Delivery is confirmed end to end** after a (re)start
  (`homelab_install.vector.confirm_delivery`).
"""

from __future__ import annotations

import grp
import subprocess

from homelab_install import env, files, log, packages, run, systemd, vector
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

_run = subprocess.run


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


def is_active() -> bool:
    return _run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0


def install(ctx: InstallContext) -> None:
    log.header("Vector Host")
    preflight(ctx)

    log.action("Vector package")
    ensure_package(ctx)
    require_journal_access()

    log.action("Vector config")
    # Before the check: the config names it as journalctl_path.
    changed = files.install(ctx, WRAPPER_NAME)
    vector.check_config(VECTOR, ctx.build_dir / CONFIG_NAME, ctx.build_dir / TESTS_NAME)
    # Each its own call, never short-circuited.
    changed = files.install(ctx, OVERRIDE_NAME) or changed
    changed = files.install(ctx, CONFIG_NAME) or changed

    starting = changed or not is_active()
    # ensure_running daemon-reloads before the restart when anything changed.
    systemd.ensure_running(ctx, UNIT, changed)
    systemd.require_active(ctx, UNIT)
    if starting:
        vector.confirm_delivery(
            ctx.env["VECTOR_QUERY_URL"], ctx.env["VECTOR_TENANT"], MARKER_TAG, UNIT
        )


if __name__ == "__main__":
    run(install, "Vector Host")
