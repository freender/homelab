#!/usr/bin/env python3
"""Remote installer for the vmalert-rules module (freender/homelab-ops#30).

The first ported module whose sources are **static configs staged outside
`build/`** -- `configs/*.yml` go to `<remote_root>/rules/`, with no per-host
render -- which is what `files.install_from` arrived for.

Three things here are not file copying, and they are the reason this module is
worth more than its line count:

* **Rules are validated before they are installed**, by running the image the
  live vmalert container uses against the staged directory with `-dryRun`. A syntactically bad
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

Two rule sets share all of the above (homelab-ops#348): the metrics rules for
`vmalert`, and the LogsQL rules in `rules/logs/` for `vmalert-logs`, which
queries VictoriaLogs. Both are validated before either is installed, so a bad
log rule cannot leave the metric rules half-deployed, and each container is
restarted only when its own set changed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from homelab_install import files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

RULES_MODE = "644"

# Indirection point for tests, matching packages.py / systemd.py.
_run = subprocess.run


@dataclass(frozen=True)
class RuleSet:
    env_key: str
    staged: str
    dest_dir: Path
    container: str
    validate_flags: tuple[str, ...] = ()


RULE_SETS = (
    RuleSet("VMALERT_RULES", "rules", Path("/mnt/cache/appdata/vmalert/rules"), "vmalert"),
    RuleSet(
        "VMALERT_LOG_RULES",
        "rules/logs",
        Path("/mnt/cache/appdata/vmalert-logs/rules"),
        "vmalert-logs",
        ("-rule.defaultRuleType=vlogs",),
    ),
)


def expected_rules(ctx: InstallContext, env_key: str) -> tuple[str, ...]:
    """The rule files the orchestrator says it staged.

    Refusing on an empty value rather than falling back to "whatever is in the
    directory" is deliberate: a missing list means the orchestrator did not
    render one, and globbing instead would turn a broken deploy into a
    successful-looking one that installs an arbitrary subset.
    """
    names = tuple(ctx.deploy_env.get(env_key, "").split())
    if not names:
        raise InstallError(f"{env_key} is empty; refusing to run")
    return names


def verify_staged(rules_dir: Path, expected: tuple[str, ...], env_key: str) -> None:
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
        raise InstallError(f"staged rules do not match {env_key} ({'; '.join(problems)})")


def verify_destination_is_managed(dest_dir: Path, expected: tuple[str, ...]) -> None:
    """Refuse if the live rules directory holds a rule this module does not own."""
    if not dest_dir.is_dir():
        raise InstallError(f"vmalert rules directory is missing: {dest_dir}")

    for path in sorted(dest_dir.glob("*.yml")):
        if path.name not in expected:
            raise InstallError(f"unmanaged active vmalert rule: {path}")


def container_image(container: str) -> str:
    """The image tag the running container uses.

    Read live rather than pinned here: a hard-coded tag drifted from v1.117.1 to
    a container on v1.155.0 unnoticed, and validating against a different
    version could accept syntax the running vmalert rejects.
    """
    result = _run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"container is missing: {container}")

    image = (result.stdout or "").strip()
    if not image:
        raise InstallError(f"could not read the image of {container}")
    return image


def validate_rules(rule_set: RuleSet, rules_dir: Path) -> None:
    """Dry-run the staged rules through the image their container runs."""
    image = container_image(rule_set.container)
    log.sub(f"Validating staged {rule_set.container} rules with {image}...")
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{rules_dir}:/rules:ro",
            image,
            "-rule=/rules/*.yml",
            *rule_set.validate_flags,
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
        raise InstallError(
            f"{rule_set.container} rejected the staged rules (exit {result.returncode})"
        )


def restart_container(container: str) -> None:
    result = _run(["docker", "restart", container], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        log.sub((result.stderr or "").strip() or "<docker returned nothing>")
        raise InstallError(f"failed to restart {container} (exit {result.returncode})")
    log.ok(f"{container} restarted")


def install_rule_set(ctx: InstallContext, rule_set: RuleSet, expected: tuple[str, ...]) -> None:
    rules_dir = ctx.script_dir / rule_set.staged
    changed = False
    for name in expected:
        if files.install_from(ctx, rules_dir / name, str(rule_set.dest_dir / name), RULES_MODE):
            changed = True

    if changed:
        log.sub(f"Restarting {rule_set.container} to load updated rules...")
        restart_container(rule_set.container)
    else:
        log.ok(f"{rule_set.container} rules unchanged")


def install(ctx: InstallContext, rule_sets: tuple[RuleSet, ...] = RULE_SETS) -> None:
    log.header("vmalert Rules")

    plan = []
    for rule_set in rule_sets:
        expected = expected_rules(ctx, rule_set.env_key)
        rules_dir = ctx.script_dir / rule_set.staged
        verify_staged(rules_dir, expected, rule_set.env_key)
        verify_destination_is_managed(rule_set.dest_dir, expected)
        validate_rules(rule_set, rules_dir)
        plan.append((rule_set, expected))

    for rule_set, expected in plan:
        install_rule_set(ctx, rule_set, expected)


if __name__ == "__main__":
    run(install, "vmalert Rules")
