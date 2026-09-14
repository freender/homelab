#!/usr/bin/env python3
"""Remote installer for the vmalert-rules module (freender/homelab-ops#30).

The first ported module whose sources are **static configs staged outside
`build/`** -- `configs/*.yml` go to `<remote_root>/rules/`, with no per-host
render -- which is what `files.install_from` arrived for.

Three things here are not file copying, and they are the reason this module is
worth more than its line count:

* **Rules are validated before they are installed**, by running the pinned
  vmalert image against the staged directory with `-dryRun`. A syntactically bad
  rule file that reached the destination would be rejected by the running vmalert
  at reload, leaving it serving the *old* rules with no obvious signal -- alerting
  silently frozen at the last good config is the worst failure this module has.
* **An unmanaged rule at the destination is a hard error.** Anything in the rules
  directory that this module did not put there is alerting the homelab with rules
  that exist in no repo, so it is refused rather than deleted: deleting it would
  destroy the only copy.
* **vmalert is restarted only when something changed.** A restart drops the
  in-memory state of every active alert, so doing it unconditionally would
  re-fire pending alerts on every deploy.

The expected rule set arrives from the orchestrator in `VMALERT_RULES` rather
than being listed here. The bash hardcoded six `require_file` calls and the
module had since grown to sixteen; the list had been stale for ten files and
nobody noticed, because the copy loop globbed the directory and never consulted
it. Keeping one list, in the orchestrator that already validates it, is the fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from homelab_install import files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

RULES_DEST_DIR = Path("/mnt/cache/appdata/vmalert/rules")
RULES_MODE = "644"
CONTAINER = "vmalert"
VMALERT_IMAGE = "victoriametrics/vmalert:v1.117.1"

# Indirection point for tests, matching packages.py / systemd.py.
_run = subprocess.run


def expected_rules(ctx: InstallContext) -> tuple[str, ...]:
    """The rule files the orchestrator says it staged.

    Refusing on an empty value rather than falling back to "whatever is in the
    directory" is deliberate: a missing `VMALERT_RULES` means the orchestrator
    did not render one, and globbing instead would turn a broken deploy into a
    successful-looking one that installs an arbitrary subset.
    """
    names = tuple(ctx.deploy_env.get("VMALERT_RULES", "").split())
    if not names:
        raise InstallError("VMALERT_RULES is empty; refusing to run")
    return names


def verify_staged(rules_dir: Path, expected: tuple[str, ...]) -> None:
    """Fail unless the staged directory is exactly the expected set.

    Catches a partial upload, which is the failure the bash's stale `require_file`
    list was groping at. Checked as a set comparison in both directions: a missing
    file means a rule silently stops being enforced, and an unexpected extra means
    a stale rule from a previous layout is about to be installed.
    """
    if not rules_dir.is_dir():
        raise InstallError(f"missing staged rules directory: {rules_dir}")

    staged = {path.name for path in rules_dir.glob("*.yml")}
    missing = sorted(set(expected) - staged)
    unexpected = sorted(staged - set(expected))

    problems = []
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected: {', '.join(unexpected)}")
    if problems:
        raise InstallError(f"staged rules do not match VMALERT_RULES ({'; '.join(problems)})")


def verify_destination_is_managed(expected: tuple[str, ...]) -> None:
    """Refuse if the live rules directory holds a rule this module does not own."""
    if not RULES_DEST_DIR.is_dir():
        raise InstallError(f"vmalert rules directory is missing: {RULES_DEST_DIR}")

    for path in sorted(RULES_DEST_DIR.glob("*.yml")):
        if path.name not in expected:
            raise InstallError(f"unmanaged active vmalert rule: {path}")


def validate_rules(rules_dir: Path) -> None:
    """Dry-run the staged rules through the pinned vmalert image.

    The image is pinned to the tag the container itself runs: validating against
    a different version could accept syntax the running vmalert rejects, which
    would defeat the point of checking at all.
    """
    log.sub("Validating staged vmalert rules...")
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{rules_dir}:/rules:ro",
            VMALERT_IMAGE,
            "-rule=/rules/*.yml",
            "-dryRun",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # vmalert reports the offending file and line on stderr; without it the
        # operator gets "validation failed" and no way to find the bad rule.
        log.sub((result.stderr or result.stdout or "").strip() or "<vmalert returned nothing>")
        raise InstallError(f"vmalert rejected the staged rules (exit {result.returncode})")


def restart_vmalert() -> None:
    result = _run(["docker", "restart", CONTAINER], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        log.sub((result.stderr or "").strip() or "<docker returned nothing>")
        raise InstallError(f"failed to restart {CONTAINER} (exit {result.returncode})")
    log.ok(f"{CONTAINER} restarted")


def install(ctx: InstallContext) -> None:
    log.header("vmalert Rules")

    expected = expected_rules(ctx)
    rules_dir = ctx.script_dir / "rules"

    verify_staged(rules_dir, expected)
    verify_destination_is_managed(expected)
    validate_rules(rules_dir)

    changed = False
    for name in expected:
        if files.install_from(
            ctx, rules_dir / name, str(RULES_DEST_DIR / name), RULES_MODE
        ):
            changed = True

    if changed:
        log.sub("Restarting vmalert to load updated rules...")
        restart_vmalert()
    else:
        log.ok("vmalert rules unchanged")


if __name__ == "__main__":
    run(install, "vmalert Rules")
