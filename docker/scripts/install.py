#!/usr/bin/env python3
"""Remote installer for the docker module (freender/homelab-ops#30).

Installs the appdata helper scripts (`start.sh`, `rm.sh`, `rebuild.sh`,
`docker-common.sh`), the env file they read, and -- on hosts with a
`docker.update_schedule` -- the `homelab-docker-update` service and timer. Every
destination comes from `build/<host>/file-map.conf`, derived from the same
`FileSpec`s the dry-run diffs against.

Worth knowing before reading:

* **The helper scripts are pinned to 755.** The bash only ever `chmod +x`ed them,
  which adds execute and never removes write, so they had drifted to 775 and 777
  on several hosts. `start.sh` is the update service's `ExecStart` and runs as
  root; a group- or world-writable copy of it is root for whoever can write it.
* **The legacy cleanup is not ported.** The bash retired nine units
  (`homelab-docker-backup.*`, `homelab-docker-start.*`,
  `homelab-docker-clean-shutdown.service`, `syncthing-{pause,unpause}.*`) and two
  files on every deploy. None of them existed on any docker host when this was
  ported -- checked on all six, not assumed.
* **A disabled timer keeps its unit files.** Same as the bash: the orchestrator
  stops rendering them and this stops the timer, but nothing is removed.
* **A redeploy retries a failed update run** -- see `systemd.recover_failed` for
  why that is the right moment and never a failed deploy.
"""

from __future__ import annotations

from homelab_install import env, files, log, run, systemd
from homelab_install.context import InstallContext

UPDATE_SERVICE = "homelab-docker-update.service"
UPDATE_TIMER = "homelab-docker-update.timer"
HELPER_FILES = ("start.sh", "rm.sh", "rebuild.sh", "docker-common.sh", "env")
TIMER_FLAG = "ENABLE_DOCKER_UPDATE_TIMER"


def install(ctx: InstallContext) -> None:
    # Required, not defaulted: an absent flag must not read as "turn the timer off".
    env.require(ctx, TIMER_FLAG)
    timer_enabled = env.flag(ctx, TIMER_FLAG)
    log.header("Docker")

    for name in HELPER_FILES:
        files.install(ctx, name)

    if not timer_enabled:
        systemd.ensure_stopped(ctx, UPDATE_TIMER)
        return

    # Both, never short-circuited: a changed timer must not skip the service.
    changed = [files.install(ctx, unit) for unit in (UPDATE_SERVICE, UPDATE_TIMER)]
    systemd.ensure_running(ctx, UPDATE_TIMER, changed=any(changed))
    systemd.recover_failed(ctx, UPDATE_SERVICE)


if __name__ == "__main__":
    run(install, "Docker")
