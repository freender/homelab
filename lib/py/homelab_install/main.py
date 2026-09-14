"""The `run()` harness -- replaces the 19-line preamble every `install.sh` opens
with (`set -e`, `HOST=${1:-$(hostname)}`, `SCRIPT_DIR`, `BUILD_DIR`, sourcing
`lib/utils.sh` or dying, `require_dir`/`require_file`).

Deliberately does **not** replicate `keepalived/scripts/install.sh:5-7`'s `sudo -n`
re-exec -- it is dead code on every target host (helm/neo/tower are all
`config.user: root`), settled in freender/homelab-ops#31.
"""

from __future__ import annotations

import argparse
import os
import shlex
import socket
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

from . import log
from .context import InstallContext
from .errors import InstallError

FILE_MAP_NAME = "file-map.conf"
ENV_NAME = "env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse `build/<host>/env` -- `KEY=<shlex.quote(value)>` lines as written
    by `write_env_file` (`src/homelab/build.py`). Parsed, never sourced: this is
    what `require_env` patches over in bash today, where a truncated env
    silently disables a flag rather than failing to parse.

    keepalived ships no env file, so this always returns `{}` for it -- but
    `ctx.env` still has to mean "the parsed build env file", not "the process
    environment", or the first module that reads `ctx.env.get("ENABLE_...")`
    inherits whatever happens to be in the calling shell instead.
    """
    env: dict[str, str] = {}
    if not path.is_file():
        return env

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        key, sep, raw_value = raw_line.partition("=")
        if not sep or not key.strip():
            raise InstallError(f"malformed env line in {path}:{line_number}: {raw_line!r}")
        tokens = shlex.split(raw_value)
        env[key.strip()] = tokens[0] if tokens else ""
    return env


def _parse_file_map(path: Path) -> dict[str, tuple[str, str]]:
    """Parse `name|dest|mode` lines -- the one format `write_file_map`
    (`src/homelab/module_support.py`) and this now share (decision 4)."""
    file_map: dict[str, tuple[str, str]] = {}
    if not path.is_file():
        return file_map

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 2 or not parts[0]:
            raise InstallError(f"malformed file-map line in {path}: {line!r}")
        name, dest = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 and parts[2] else "644"
        file_map[name] = (dest, mode)
    return file_map


def run(
    install_fn: Callable[[InstallContext], None],
    module_name: str,
    require_root: bool = True,
) -> None:
    """Parse `[host] [--force]`, build the `InstallContext`, call `install_fn`,
    and turn its outcome into the same exit codes and footer `install.sh` gave.

    `module_name` is explicit rather than derived from the script's directory --
    a prototype-forced change from the design doc's `run(install)` signature.
    Deriving it from `script_dir.name.title()` breaks for hyphenated multi-word
    modules (`pve-postinstall-webhook` -> "Pve-Postinstall-Webhook", not
    "PVE Postinstall Webhook"), and the footer needs the same string `install_fn`
    already passes to `log.header()` at the top of its own run.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("host", nargs="?", default=socket.gethostname())
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    # `require_root=False` is not "root optional" -- it is for the modules whose
    # target is the *deploy user's* own home, where running as root would write
    # root-owned files into it and break the next non-root deploy. `ssh-config`
    # installs `~/.ssh/config` and is the first such caller; its orchestrator
    # already stages with `require_root=False`. Everything else stays root-only,
    # since the failure it catches is a silent one: writing to /etc as a normal
    # user fails per-file, late, after some of the bundle has already applied.
    if require_root and os.geteuid() != 0:
        log.error("must be run as root")
        sys.exit(1)

    script_dir = Path(sys.argv[0]).resolve().parent.parent
    build_dir = script_dir / "build" / args.host

    # FORCE_UPDATE arrives in the process environment, not the build/<host>/env
    # file: stage_and_run_remote_installer always passes it via `env=`, and the
    # bash re-exec that used to strip it under `sudo -n` is the dead code above.
    # This is a different source than ctx.env below -- see _parse_env_file. It is
    # the same source as ctx.deploy_env, of which this is a parsed convenience
    # over one key.
    force_update = args.force or os.environ.get("FORCE_UPDATE", "false").lower() == "true"

    ctx = InstallContext(
        host=args.host,
        script_dir=script_dir,
        build_dir=build_dir,
        env=_parse_env_file(build_dir / ENV_NAME),
        deploy_env=dict(os.environ),
        file_map=_parse_file_map(build_dir / FILE_MAP_NAME),
        force_update=force_update,
    )

    try:
        install_fn(ctx)
    except InstallError as exc:
        log.error(str(exc))
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(2)

    log.header(f"{module_name} Complete")
