#!/usr/bin/env python3
"""Remote installer for the docker-stacks module (freender/homelab-ops#30).

Syncs repo-managed `compose.yml` files into the appdata root and runs
`docker compose up -d` on the stacks whose definition actually changed.

Stack name == directory name, both in the repo and on the host, so the compose
project name stays identical to what `start.sh` already creates. This module owns
`compose.yml` and nothing else: the stack's `.env`, config and data are host-local.
The `.env` is read for its variable *names* only, never for their values, and it
is never written or removed.

Every refusal is per stack and leaves that stack's host copy untouched. The other
stacks still sync, and the deploy fails at the end. Refused, in order:

* **No appdata directory.** It does not mean "new stack". It means the stack is
  declared on a host that has none of its state, almost always a placement moved
  in `hosts.conf` without the data. Creating it would start containers against
  no config. New stacks are onboarded by creating the directory and its `.env`
  on the host first.
* **A `${VAR}` nothing defines.** Compose substitutes empty for an undefined
  variable and carries on, which turns ``Host(`x.${DOMAIN}`)`` into ``Host(`x.`)``
  and silently unroutes the service.
* **A definition `docker compose config` rejects.** See below.
* **A renamed or removed service with a container still present.** `up -d`
  creates the new container and leaves the old one running. For crowdsec that is
  two LAPI instances on one SQLite database; for cloudflared, two connectors on
  one tunnel.

Behaviour changes from the bash, worth knowing before reading:

* **A definition compose cannot parse is no longer installed.** The bash
  discarded a failed `config --services` and went on to copy the file, so a
  broken compose file replaced the working one on the host and then failed to
  apply. `start.sh` and the update timer read that same file, so the stack's next
  restart was broken as well. The host copy is now left alone.
* **A failed `docker ps` fails the stack.** The bash read it as "no containers",
  which skipped the rename check exactly when it could not be done.
* **Missing docker fails the deploy up front.** The bash then skipped the rename
  check silently, still copied every file and failed only at apply. With
  `APPLY_CHANGED=false` it reported success for a check it never ran.
* **An escaped `$${VAR}` is not a variable.** Compose passes it through to the
  container as a literal `${VAR}`. The bash grep matched it anyway, so
  `alertmanager` and `immich` were only deployable because their `.env` happened
  to define the container-side names too.
* **A partial upload is refused.** `MANAGED_STACK_COUNT` was written by the
  orchestrator and read by nothing. A stack missing from the staged tree just did
  not sync, and then showed up as "unmanaged" on the host.
* **Modes and owners are kept.** A changed `compose.yml` is written in place, as
  `cp` did. A new one takes the owner of the stack directory it lands in, not
  root, since the directory was created by hand for the onboarding.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from homelab_install import env, files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

COMPOSE_NAME = "compose.yml"

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which

# `${NAME}` with no inline default or error form. `${VAR:-y}` and `${VAR:?msg}`
# carry their own answer and are excluded by the pattern, as in the bash. The
# run of dollars in front decides escaping: `$${X}` is a literal, `$$${X}` is a
# literal `$` followed by a real `${X}`.
_REFERENCE = re.compile(r"(\$+)\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


class Tally:
    """Per-run outcome counts for the summary line."""

    def __init__(self) -> None:
        self.changed = 0
        self.applied = 0
        self.skipped = 0
        self.failed = 0


def referenced_variables(compose_text: str) -> set[str]:
    """Names compose will interpolate from this file."""
    return {name for dollars, name in _REFERENCE.findall(compose_text) if len(dollars) % 2 == 1}


def defined_variables(env_file: Path) -> set[str]:
    """Names the stack's `.env` defines. Values are never read into memory as such,
    only matched for a key, and an empty `NAME=` still counts, as in the bash."""
    if not env_file.is_file():
        return set()
    return {
        match.group(1)
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if (match := _ENV_KEY.match(line))
    }


def missing_variables(ctx: InstallContext, compose: Path, env_file: Path) -> list[str]:
    referenced = referenced_variables(compose.read_text(encoding="utf-8"))
    defined = defined_variables(env_file)
    return sorted(
        name for name in referenced if name not in defined and not ctx.deploy_env.get(name)
    )


def defined_services(docker: str, compose: Path, dest_dir: Path) -> list[str]:
    """The incoming file's service list, with `.env` resolved from the stack's own
    directory. Raises when compose rejects the file."""
    result = _run(
        [
            docker,
            "compose",
            "-f",
            str(compose),
            "--project-directory",
            str(dest_dir),
            "config",
            "--services",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"exit {result.returncode}"
        raise InstallError(f"docker compose config rejected the definition: {reason}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def stale_containers(docker: str, stack: str, services: list[str]) -> list[str]:
    """Containers of this compose project whose service the incoming file no
    longer defines, i.e. a renamed or deleted service."""
    result = _run(
        [
            docker,
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={stack}",
            "--format",
            '{{.Label "com.docker.compose.service"}}|{{.Names}}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise InstallError(f"docker ps failed (exit {result.returncode}); cannot check for renames")

    known = set(services)
    stale = []
    for line in result.stdout.splitlines():
        service, _sep, name = line.partition("|")
        if service and service not in known:
            stale.append(name)
    return stale


def refusal(ctx: InstallContext, docker: str, stack: str, compose: Path, dest_dir: Path) -> bool:
    """Run the pre-copy checks. Returns True, after warning, when the stack must be
    left alone. Raises `InstallError` when a check could not be performed."""
    if not dest_dir.is_dir():
        log.warn(f"{stack}: {dest_dir} does not exist on this host")
        log.warn("  Refusing to create it. A repo-managed stack with no appdata directory")
        log.warn("  usually means it was moved to another host without its config/.env/data.")
        log.warn("  New stack? Create the directory and its .env on the host, then redeploy.")
        return True

    missing = missing_variables(ctx, compose, dest_dir / ".env")
    if missing:
        log.warn(
            f"{stack} needs undefined variable(s): {' '.join(missing)}; leaving host copy untouched"
        )
        return True

    try:
        services = defined_services(docker, compose, dest_dir)
    except InstallError as exc:
        log.warn(f"{stack}: {exc}; leaving host copy untouched")
        return True

    stale = stale_containers(docker, stack, services)
    if stale:
        names = " ".join(stale)
        log.warn(f"{stack} renames/removes running container(s): {names}")
        log.warn("  docker compose up -d would leave them running alongside the new ones.")
        log.warn(f"  Remove them first:  docker rm -f {names}")
        log.warn("  Leaving host copy untouched.")
        return True
    return False


def install_compose(ctx: InstallContext, stack: str, compose: Path, dest: Path) -> bool:
    """Write the file in place, keeping mode and owner. A new file takes its
    directory's owner."""
    created = not dest.exists()
    changed = files.install_from(ctx, compose, str(dest), None, record=stack)
    if created:
        owner = dest.parent.stat()
        os.chown(dest, owner.st_uid, owner.st_gid)
    return changed


def apply_stack(docker: str, stack: str, dest_dir: Path) -> bool:
    log.sub(f"Applying {stack}...")
    result = _run([docker, "compose", "up", "-d"], cwd=dest_dir, check=False)
    if result.returncode == 0:
        log.ok(f"{stack} applied")
        return True
    log.error(f"{stack} failed to apply")
    return False


def sync_stack(
    ctx: InstallContext,
    docker: str,
    appdata_root: Path,
    apply_changed: bool,
    stack: str,
    tally: Tally,
) -> None:
    compose = ctx.build_dir / "stacks" / stack / COMPOSE_NAME
    dest_dir = appdata_root / stack

    try:
        if refusal(ctx, docker, stack, compose, dest_dir):
            tally.skipped += 1
            return
        changed = install_compose(ctx, stack, compose, dest_dir / COMPOSE_NAME)
    except (InstallError, OSError) as exc:
        log.error(f"{stack}: {exc}")
        tally.failed += 1
        return

    if not changed:
        return
    tally.changed += 1
    if not apply_changed:
        log.sub(f"{stack} changed; apply disabled, not reconciled")
    elif apply_stack(docker, stack, dest_dir):
        tally.applied += 1
    else:
        tally.failed += 1


def staged_stacks(ctx: InstallContext) -> list[str]:
    """The stacks in the uploaded tree, checked against the count the orchestrator
    rendered so a partial upload cannot pass for a smaller stack list."""
    stacks_dir = ctx.build_dir / "stacks"
    if not stacks_dir.is_dir():
        raise InstallError(f"missing staged stacks directory: {stacks_dir}")

    raw_count = ctx.env["MANAGED_STACK_COUNT"]
    try:
        expected = int(raw_count)
    except ValueError as exc:
        raise InstallError(f"MANAGED_STACK_COUNT must be an integer, got {raw_count!r}") from exc

    stacks = sorted(path.parent.name for path in stacks_dir.glob(f"*/{COMPOSE_NAME}"))
    if len(stacks) != expected:
        raise InstallError(
            f"staged {len(stacks)} stack(s) but the orchestrator rendered {expected}; "
            "refusing a partial upload"
        )
    return stacks


def install(ctx: InstallContext) -> None:
    # An empty flag would silently downgrade this to a file copy with no reconcile,
    # which looks like a successful deploy while leaving containers on the old
    # definition. Refuse to run on a truncated env instead.
    env.require(ctx, "APPDATA_ROOT", "APPLY_CHANGED", "MANAGED_STACK_COUNT")
    apply_changed = env.flag(ctx, "APPLY_CHANGED")
    appdata_root = Path(ctx.env["APPDATA_ROOT"])
    stacks = staged_stacks(ctx)
    if not appdata_root.is_dir():
        raise InstallError(f"missing appdata root: {appdata_root}")
    docker = _which("docker")
    if docker is None:
        raise InstallError("docker not found; cannot check or apply any stack")

    log.header("Docker Stacks")

    tally = Tally()
    for stack in stacks:
        sync_stack(ctx, docker, appdata_root, apply_changed, stack, tally)

    # Stacks the host runs that the repo does not describe. Reported so the drift
    # is visible; never removed, because repo coverage is deliberately partial.
    on_host = {path.parent.name for path in appdata_root.glob(f"*/{COMPOSE_NAME}")}
    orphans = sorted(on_host - set(stacks))
    if orphans:
        log.warn(f"unmanaged stacks on host (not in repo, left untouched): {' '.join(orphans)}")

    log.action(
        f"managed={len(stacks)} changed={tally.changed} applied={tally.applied} "
        f"skipped={tally.skipped} failed={tally.failed} unmanaged={len(orphans)}"
    )

    if tally.failed:
        raise InstallError(f"{tally.failed} stack(s) failed")
    if tally.skipped:
        raise InstallError(
            f"{tally.skipped} stack(s) skipped; see warnings above (missing appdata "
            "directory, undefined variable, rejected definition, or a rename needing "
            "manual container removal)"
        )


if __name__ == "__main__":
    run(install, "Docker Stacks")
