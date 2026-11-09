#!/usr/bin/env python3
"""Remote installer for the vector-config module (freender/homelab-ops#366).

The same shape as the retired `alloy-config`: check the staged config against
the image the running container uses, install only if the bytes differ, reload
only if they did.

* **Validated and unit-tested in the running image.** `vector validate
  --no-environment` checks the config without touching the Docker socket or
  VictoriaLogs; `vector test` then runs the rendered `vector.test.yaml`. Both
  use the GeoIP database the container mounts, so the ASN lookup is tested
  against the real file.
* **The file is written in place.** `vector.yaml` is a single-file bind mount,
  and a bind mount pins the inode: a rename would leave the container reading
  the old file forever.
* **Reload is SIGHUP, then confirmed from the log.** A rejected reload leaves
  Vector running the *old* config, so the installer waits for `Vector has
  reloaded.` and fails the deploy on a reload error or on silence.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from homelab_install import env, files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

CONFIG_NAME = "vector.yaml"
TESTS_NAME = "vector.test.yaml"
CONTAINER_CONFIG = "/etc/vector/vector.yaml"
CONTAINER_TESTS = "/etc/vector/vector.test.yaml"
CONTAINER_GEOIP = "/etc/vector/geoip"
RELOADED = "Vector has reloaded."
# A config that fails to load, a topology that rolled back, and a reload that
# killed Vector outright, in Vector 0.61's wording.
RELOAD_FAILED = ("reload aborted", "Reload was not successful", "Fatal error during reload")
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
        raise InstallError(f"missing live Vector config: {dest}")


def check_config(container: str, image: str, config: Path, tests: Path, geoip_dir: str) -> None:
    # Named under the running container's prefix, which its docker_logs source
    # excludes, so the check's own output is not shipped as a container's logs.
    args = [
        "run", "--rm", "--name", f"{container}-check",
        "-v", f"{config}:{CONTAINER_CONFIG}:ro",
        "-v", f"{tests}:{CONTAINER_TESTS}:ro",
    ]
    if geoip_dir:
        args += ["-v", f"{geoip_dir}:{CONTAINER_GEOIP}:ro"]

    log.sub("Validating staged Vector config...")
    result = _docker(*args, image, "validate", "--no-environment", CONTAINER_CONFIG)
    if result.returncode != 0:
        _report(result)
        raise InstallError(f"vector rejected the staged config (exit {result.returncode})")

    log.sub("Running Vector unit tests...")
    result = _docker(*args, image, "test", CONTAINER_CONFIG, CONTAINER_TESTS)
    if result.returncode != 0:
        _report(result)
        raise InstallError(f"vector unit tests failed (exit {result.returncode})")


def reload_vector(container: str) -> None:
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
        if any(marker in output for marker in RELOAD_FAILED):
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
    log.header("Vector Config")

    env.require(ctx, "VECTOR_CONTAINER", "VECTOR_DEST")
    container = ctx.env["VECTOR_CONTAINER"]
    dest = ctx.env["VECTOR_DEST"]
    geoip_dir = ctx.env.get("VECTOR_GEOIP_DIR", "")

    src = ctx.build_dir / CONFIG_NAME
    tests = ctx.build_dir / TESTS_NAME
    for staged in (src, tests):
        if not staged.is_file():
            raise InstallError(f"missing staged file: {staged}")
    require_live(dest)

    check_config(container, container_image(container), src, tests, geoip_dir)

    # mode=None: this module owns the file's content, not its permissions.
    if files.install_from(ctx, src, dest, None):
        reload_vector(container)
    else:
        log.ok("Vector config unchanged")


if __name__ == "__main__":
    run(install, "Vector Config")
