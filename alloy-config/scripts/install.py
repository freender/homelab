#!/usr/bin/env python3
"""Remote installer for the alloy-config module (freender/homelab-ops#343).

Same shape as `monitoring-config`: validate against the image the running
container uses, install only if the bytes differ, reload only if they did. Two
details are specific to Alloy:

* **The file is written in place.** `config.alloy` is a single-file bind mount,
  and a bind mount pins the inode: replacing the file by rename would leave the
  container reading the old one forever. `files.install_from` writes in place.
* **Reload is SIGHUP, then confirmed from the log.** The Alloy image ships no
  wget or curl to POST `/-/reload` from inside, and its port is not published.
  A rejected reload leaves Alloy running the *old* config with no other signal,
  so the installer waits for Alloy's own `config reloaded` line and fails the
  deploy on `failed to reload config` or on silence.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from homelab_install import env, files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

CONFIG_NAME = "config.alloy"
CONTAINER_CONFIG = "/etc/alloy/config.alloy"
RELOADED = "config reloaded"
RELOAD_FAILED = "failed to reload config"
RELOAD_TIMEOUT_SECONDS = 15.0
RELOAD_POLL_SECONDS = 1.0

# Indirection points for tests, matching packages.py / systemd.py.
_run = subprocess.run
_sleep = time.sleep
_now = time.monotonic


def _docker(*args: str) -> subprocess.CompletedProcess:
    return _run(["docker", *args], check=False, capture_output=True, text=True)


def _report(result: subprocess.CompletedProcess) -> None:
    log.sub((result.stderr or result.stdout or "").strip() or "<no output>")


def container_image(container: str) -> str:
    """The image tag the named container runs; doubles as the existence check."""
    result = _docker("inspect", "--format", "{{.Config.Image}}", container)
    if result.returncode != 0:
        raise InstallError(f"container is missing: {container}")

    image = (result.stdout or "").strip()
    if not image:
        raise InstallError(f"could not read the image of {container}")
    return image


def require_live(dest: str) -> None:
    """A mount check, not a bootstrap check -- see monitoring-config's twin.

    Writing through a missing appdata mount would succeed against the empty
    directory underneath while the container kept its old config.
    """
    if not Path(dest).is_file():
        raise InstallError(f"missing live Alloy config: {dest}")


def validate_config(image: str, src: Path) -> None:
    log.sub("Validating staged Alloy config...")
    result = _docker(
        "run", "--rm", "-v", f"{src}:{CONTAINER_CONFIG}:ro", image, "validate", CONTAINER_CONFIG
    )
    if result.returncode != 0:
        _report(result)
        raise InstallError(f"alloy rejected the staged config (exit {result.returncode})")


def reload_alloy(container: str) -> None:
    log.sub(f"Reloading {container}...")
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    result = _docker("kill", "--signal", "HUP", container)
    if result.returncode != 0:
        _report(result)
        raise InstallError(f"failed to signal {container} (exit {result.returncode})")

    deadline = _now() + RELOAD_TIMEOUT_SECONDS
    while True:
        logs = _docker("logs", "--since", since, container)
        output = f"{logs.stdout or ''}{logs.stderr or ''}"
        if RELOAD_FAILED in output:
            log.sub(output.strip())
            raise InstallError(f"{container} rejected the new config and kept the old one")
        if RELOADED in output:
            log.ok(f"{container} reloaded")
            return
        if _now() >= deadline:
            raise InstallError(
                f"{container} did not confirm a reload within {RELOAD_TIMEOUT_SECONDS:.0f}s"
            )
        _sleep(RELOAD_POLL_SECONDS)


def install(ctx: InstallContext) -> None:
    log.header("Alloy Config")

    env.require(ctx, "ALLOY_CONTAINER", "ALLOY_DEST")
    container = ctx.env["ALLOY_CONTAINER"]
    dest = ctx.env["ALLOY_DEST"]

    src = ctx.build_dir / CONFIG_NAME
    if not src.is_file():
        raise InstallError(f"missing staged Alloy config: {src}")
    require_live(dest)

    validate_config(container_image(container), src)

    # mode=None: the live files are 666 on tower and differ elsewhere; this
    # module owns their content, not their permissions.
    if files.install_from(ctx, src, dest, None):
        reload_alloy(container)
    else:
        log.ok("Alloy config unchanged")


if __name__ == "__main__":
    run(install, "Alloy Config")
